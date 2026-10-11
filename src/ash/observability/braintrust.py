"""Bounded, scrubbed records shared by production observations and evals."""

from __future__ import annotations

import re
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from ash.llm.types import ToolResult
from ash.observability import _scrub_sentry_value

if TYPE_CHECKING:
    from ash.config.models import BraintrustConfig
    from ash.core.session import SessionState

_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_CREDENTIAL = re.compile(
    r"(?i)([\w.-]*(?:api[_-]?key|token|password|secret)[\w.-]*"
    r"[\"']?\s*[:=]\s*[\"']?)[^\s\"',;}]+"
)
_KEY = re.compile(r"\b(?:sk-|bt-)[A-Za-z0-9_-]{16,}\b")


def scrub(value: Any, limit: int = 4000) -> Any:
    """Best-effort secret/PII filtering, before truncation of each string."""
    value = _scrub_sentry_value(value)
    if isinstance(value, dict):
        return {
            str(k): (
                "[Filtered]"
                if re.search(
                    r"api.?key|token|password|secret|authorization|cookie", str(k), re.I
                )
                else scrub(v, limit)
            )
            for k, v in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [scrub(v, limit) for v in value]
    if isinstance(value, str):
        value = _KEY.sub("[Filtered]", value)
        value = _CREDENTIAL.sub(r"\1[Filtered]", value)
        value = _EMAIL.sub("[email]", value)
        return value if len(value) <= limit else value[:limit] + "[truncated]"
    return value


def conversation_record(
    user_message: str,
    session: SessionState,
    config: BraintrustConfig,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Observe retained context without equating session text with delivery."""
    messages = session.messages
    # Match only textual user messages, never user-role tool results.
    start = next(
        (
            i
            for i in range(len(messages) - 1, -1, -1)
            if messages[i].role == "user"
            and isinstance(messages[i].content, str)
            and messages[i].content == user_message
        ),
        None,
    )
    turn = messages[start:] if start is not None else []
    calls = [call for m in turn for call in m.get_tool_uses()]
    results = [
        block
        for m in turn
        if isinstance(m.content, list)
        for block in m.content
        if isinstance(block, ToolResult)
    ]
    retained = messages[-config.max_messages :]
    record: dict[str, Any] = {
        "metadata": {
            **metadata,
            "conversation_id": session.session_id,
            "provider": session.provider,
            "observation_kind": "post_turn_session_snapshot",
            "turn_boundary_found": start is not None,
            "history_truncated": len(retained) < len(messages),
            "content_captured": config.capture_content,
            "tool_names": [c.name for c in calls],
        },
        "metrics": {
            "tool_calls": len(calls),
            "tool_errors": sum(r.is_error for r in results),
        },
        "tags": ["ash", "conversation"],
    }
    if config.capture_content:
        record["input"] = {"request": user_message}
        record["output"] = {"retained_messages": [asdict(m) for m in retained]}
    return scrub(record, config.max_content_chars)
