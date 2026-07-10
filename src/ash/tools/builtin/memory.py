"""First-class memory tools for Ash.

Provides model-callable tools for memory management so the agent can
remember, list, search, and forget memories directly without shelling
out to the sandbox.  These wrap the Store / MemoryExtractor API that
already backs the passive-extraction pipeline.

Tools
-----
- remember        : Store an explicit memory (with LLM classification + reject-on-secret)
- list_memories   : List active memories for the current user
- search_memories : Vector-search memories for the current user
- forget_memory   : Archive one memory by id or short prefix
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

# ash.memory.secrets and ash.store.types are imported lazily inside execute()
# to avoid circular imports through ash.memory.__init__ -> ash.core.agent -> ash.tools
from ash.tools.base import Tool, ToolContext, ToolResult

if TYPE_CHECKING:
    from ash.memory.extractor import MemoryExtractor
    from ash.store.store import Store

logger = logging.getLogger(__name__)

# Human-readable labels for reject reasons returned to the model.
_REJECT_LABELS: dict[str, str] = {
    "reject_secret": "credentials / secrets cannot be stored",
    "sensitive": "sensitive personal information (medical, financial, etc.)",
    "private_to_conversation": "marked private to this conversation only",
}

_DEFAULT_LIST_LIMIT = 20
_DEFAULT_SEARCH_LIMIT = 10
_MAX_LIMIT = 50


class RememberTool(Tool):
    """Store an explicit user-directed memory.

    Runs the fact through the extractor's classify_fact() for type/subject/
    sensitivity inference, then rejects secrets before persisting.
    """

    def __init__(self, store: "Store", extractor: "MemoryExtractor | None") -> None:
        self._store = store
        self._extractor = extractor

    @property
    def name(self) -> str:
        return "remember"

    @property
    def description(self) -> str:
        return (
            "Store a fact in long-term memory. "
            "Use this when the user explicitly asks you to remember something. "
            "Do NOT use for passive observations — memory extraction handles those automatically. "
            "The content must be self-contained (include the subject when it matters). "
            "Credentials, secrets, and sensitive personal details will be rejected."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The fact to remember. Write in third person if about the user (e.g. 'User prefers dark mode').",
                    "minLength": 1,
                    "maxLength": 4000,
                },
                "expires_in_days": {
                    "type": "integer",
                    "description": "Optional: number of days until the memory expires. Omit for permanent storage.",
                    "minimum": 1,
                    "maximum": 3650,
                },
            },
            "required": ["content"],
        }

    async def execute(self, input_data: dict[str, Any], context: ToolContext) -> ToolResult:
        from ash.memory.secrets import contains_secret  # lazy — avoids circular import
        from ash.store.types import DisclosureClass, MemoryType  # lazy

        content = (input_data.get("content") or "").strip()
        if not content:
            return ToolResult.error("Memory content is required.")

        expires_in_days: int | None = input_data.get("expires_in_days")

        # Fast-path secret guard (regex-based, no LLM call)
        if contains_secret(content):
            return ToolResult.error(
                "Memory not stored: content appears to contain credentials or secrets. "
                "Never store API keys, passwords, tokens, or private keys."
            )

        # LLM classification (type, sensitivity, disclosure, subjects)
        memory_type: MemoryType | None = None
        sensitivity = None
        if self._extractor is not None:
            try:
                fact = await self._extractor.classify_fact(content)
                if fact is None:
                    # Extractor returned None — likely rejected as secret/sensitive.
                    return ToolResult.error(
                        "Memory not stored: the content was classified as a secret or "
                        "otherwise unsuitable for long-term storage."
                    )
                # Reject explicit secret/private-to-conversation classifications
                if fact.disclosure == DisclosureClass.REJECT_SECRET:
                    label = _REJECT_LABELS.get("reject_secret", "not suitable for storage")
                    return ToolResult.error(f"Memory not stored: {label}.")

                memory_type = fact.memory_type
                sensitivity = fact.sensitivity
            except Exception:
                logger.warning("remember_tool_classify_failed", exc_info=True)
                # Fall through — store with defaults rather than blocking the user

        try:
            entry = await self._store.add_memory(
                content=content,
                source="user",
                memory_type=memory_type,
                sensitivity=sensitivity,
                expires_in_days=expires_in_days,
                owner_user_id=context.user_id,
                chat_id=context.chat_id,
                graph_chat_id=context.chat_id,
            )
        except ValueError as exc:
            return ToolResult.error(f"Memory not stored: {exc}")
        except Exception:
            logger.exception("remember_tool_store_failed")
            return ToolResult.error("Failed to store memory due to an internal error.")

        kind_label = entry.memory_type.value if entry.memory_type else "knowledge"
        expiry_label = ""
        if entry.expires_at:
            expiry_label = f" (expires {entry.expires_at.strftime('%Y-%m-%d')})"
        short_id = entry.id[:8]
        return ToolResult.success(
            f"Remembered [{kind_label}]{expiry_label}: {entry.content}  (id: {short_id})"
        )


class ListMemoriesTool(Tool):
    """List active memories for the current user."""

    def __init__(self, store: "Store") -> None:
        self._store = store

    @property
    def name(self) -> str:
        return "list_memories"

    @property
    def description(self) -> str:
        return (
            "List active memories stored for the current user. "
            "Use when the user asks what you remember about them, "
            "or when you need memory ids before calling forget_memory."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": f"Maximum number of memories to return (1–{_MAX_LIMIT}). Defaults to {_DEFAULT_LIST_LIMIT}.",
                    "minimum": 1,
                    "maximum": _MAX_LIMIT,
                },
            },
        }

    async def execute(self, input_data: dict[str, Any], context: ToolContext) -> ToolResult:
        raw_limit = input_data.get("limit", _DEFAULT_LIST_LIMIT)
        limit = max(1, min(_MAX_LIMIT, int(raw_limit)))

        try:
            memories = await self._store.list_memories(
                limit=limit,
                owner_user_id=context.user_id,
            )
        except Exception:
            logger.exception("list_memories_tool_failed")
            return ToolResult.error("Failed to list memories due to an internal error.")

        if not memories:
            return ToolResult.success("No memories stored yet.")

        lines: list[str] = [f"Stored memories ({len(memories)} shown):"]
        for m in memories:
            short_id = m.id[:8]
            kind = m.memory_type.value if m.memory_type else "?"
            date = (m.created_at or datetime.now(UTC)).strftime("%Y-%m-%d")
            expiry = f" [expires {m.expires_at.strftime('%Y-%m-%d')}]" if m.expires_at else ""
            lines.append(f"- [{kind}]{expiry} {m.content}  (id: {short_id}, added: {date})")

        return ToolResult.success("\n".join(lines))


class SearchMemoriesTool(Tool):
    """Vector-search memories for the current user."""

    def __init__(self, store: "Store") -> None:
        self._store = store

    @property
    def name(self) -> str:
        return "search_memories"

    @property
    def description(self) -> str:
        return (
            "Search active memories for the current user using semantic similarity. "
            "Use when the injected memory context is insufficient or when the user "
            "asks about a specific topic from their history."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language search query.",
                    "minLength": 1,
                },
                "limit": {
                    "type": "integer",
                    "description": f"Maximum number of results (1–{_MAX_LIMIT}). Defaults to {_DEFAULT_SEARCH_LIMIT}.",
                    "minimum": 1,
                    "maximum": _MAX_LIMIT,
                },
            },
            "required": ["query"],
        }

    async def execute(self, input_data: dict[str, Any], context: ToolContext) -> ToolResult:
        query = (input_data.get("query") or "").strip()
        if not query:
            return ToolResult.error("Search query is required.")

        raw_limit = input_data.get("limit", _DEFAULT_SEARCH_LIMIT)
        limit = max(1, min(_MAX_LIMIT, int(raw_limit)))

        try:
            results = await self._store.search(
                query=query,
                limit=limit,
                owner_user_id=context.user_id,
            )
        except Exception:
            logger.exception("search_memories_tool_failed")
            return ToolResult.error("Failed to search memories due to an internal error.")

        if not results:
            return ToolResult.success(f"No memories found matching '{query}'.")

        lines: list[str] = [f"Search results for '{query}' ({len(results)} found):"]
        for r in results:
            short_id = r.id[:8]
            meta = r.metadata or {}
            kind = meta.get("memory_type", "?")
            subject = f" (about {meta['subject_name']})" if meta.get("subject_name") else ""
            score = f"{r.similarity:.2f}"
            lines.append(f"- [{kind}]{subject} {r.content}  (id: {short_id}, score: {score})")

        return ToolResult.success("\n".join(lines))


class ForgetMemoryTool(Tool):
    """Archive (forget) one memory by id or short id prefix."""

    def __init__(self, store: "Store") -> None:
        self._store = store

    @property
    def name(self) -> str:
        return "forget_memory"

    @property
    def description(self) -> str:
        return (
            "Forget (permanently archive) one memory. "
            "Pass the full id or an unambiguous short prefix from list_memories or search_memories. "
            "Use only when the user explicitly asks to forget or remove something."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Full memory id or unambiguous short prefix (≥4 chars).",
                    "minLength": 4,
                },
            },
            "required": ["id"],
        }

    async def execute(self, input_data: dict[str, Any], context: ToolContext) -> ToolResult:
        memory_id = (input_data.get("id") or "").strip()
        if not memory_id:
            return ToolResult.error("Memory id is required.")

        try:
            deleted = await self._store.delete_memory(
                memory_id=memory_id,
                owner_user_id=context.user_id,
            )
        except Exception:
            logger.exception("forget_memory_tool_failed")
            return ToolResult.error("Failed to forget memory due to an internal error.")

        if not deleted:
            return ToolResult.error(
                f"Memory '{memory_id}' not found or you do not have permission to remove it. "
                "Use list_memories or search_memories to find the correct id."
            )

        return ToolResult.success(f"Memory '{memory_id}' has been forgotten.")
