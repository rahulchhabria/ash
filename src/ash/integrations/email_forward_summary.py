"""Email-forward-summary integration contributor.

Augments inbound Telegram messages that are replies to an email summary
sent by the email-forward-summary skill with structured context from the
skill's local SQLite store, so the agent can answer follow-up questions
about a specific forwarded email.

Spec contract: specs/subsystems.md (Integration Hooks),
specs/email_forward_summary.md.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ash.integrations.runtime import IntegrationContext, IntegrationContributor

if TYPE_CHECKING:
    from ash.providers.base import IncomingMessage

logger = logging.getLogger("email_forward_summary")


CONTEXT_HEADER = "Email-forward-summary context (reply target)"
CONTEXT_HEADER_RECENT = "Email-forward-summary context (most recent email)"
CONTEXT_FOOTER = "End email-forward-summary context"


def _resolve_db_path(raw: Path) -> Path | None:
    path = raw.expanduser()
    if not path.exists():
        return None
    return path


class EmailForwardSummaryIntegration(IntegrationContributor):
    """Inject email context when the user replies to an email summary message."""

    name = "email_forward_summary"
    priority = 170

    def __init__(self) -> None:
        self._db_path: Path | None = None
        self._max_body_chars: int = 4000
        self._enabled: bool = False

    async def setup(self, context: IntegrationContext) -> None:
        config = context.config.email_forward_summary
        if not config.enabled:
            return
        if config.database_path is None:
            logger.warning(
                "email_forward_summary_disabled",
                extra={"reason": "database_path_unset"},
            )
            return
        path = _resolve_db_path(Path(config.database_path))
        if path is None:
            logger.warning(
                "email_forward_summary_disabled",
                extra={
                    "reason": "database_missing",
                    "email_forward_summary.database_path": str(config.database_path),
                },
            )
            return
        self._db_path = path
        self._max_body_chars = config.max_body_chars
        self._enabled = True
        logger.info(
            "email_forward_summary_ready",
            extra={"email_forward_summary.database_path": str(path)},
        )

    async def preprocess_incoming_message(
        self,
        message: IncomingMessage,
        context: IntegrationContext,
    ) -> IncomingMessage:
        if not self._enabled or self._db_path is None:
            return message

        reply_to = message.reply_to_message_id
        row: dict[str, Any] | None = None
        source: str = ""

        if reply_to:
            try:
                tg_message_id = int(reply_to)
            except (TypeError, ValueError):
                tg_message_id = None
            if tg_message_id is not None:
                try:
                    row = self._lookup_email(tg_message_id)
                except sqlite3.Error as exc:
                    logger.warning(
                        "email_forward_summary_lookup_failed",
                        extra={
                            "error.message": str(exc),
                            "email_forward_summary.telegram_message_id": tg_message_id,
                        },
                    )
                if row is not None:
                    source = "reply"

        if row is None:
            try:
                row = self._lookup_most_recent_email()
            except sqlite3.Error as exc:
                logger.warning(
                    "email_forward_summary_recent_lookup_failed",
                    extra={"error.message": str(exc)},
                )
            if row is not None:
                source = "recent"

        if row is None:
            return message

        header = CONTEXT_HEADER if source == "reply" else CONTEXT_HEADER_RECENT
        context_block = self._render_context_block(row, header=header)
        if not context_block:
            return message

        prefixed = f"{context_block}\n\n{message.text}".strip()
        message.text = prefixed
        message.metadata = {
            **message.metadata,
            "email_forward_summary.email_id": row["id"],
            "email_forward_summary.subject": row["subject"] or "",
            "email_forward_summary.source": source,
        }
        logger.info(
            "email_forward_summary_context_injected",
            extra={
                "email_forward_summary.email_id": row["id"],
                "email_forward_summary.source": source,
            },
        )
        return message

    def _lookup_email(self, telegram_message_id: int) -> dict[str, Any] | None:
        assert self._db_path is not None
        with sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT id, subject, sender, received_at, cleaned_body,
                       structured_parse_json, processing_status
                FROM emails
                WHERE telegram_message_id = ?
                  AND processing_status = 'delivered'
                ORDER BY id DESC
                LIMIT 1
                """,
                (telegram_message_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}

    def _lookup_most_recent_email(self) -> dict[str, Any] | None:
        assert self._db_path is not None
        with sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT id, subject, sender, received_at, cleaned_body,
                       structured_parse_json, processing_status
                FROM emails
                WHERE processing_status = 'delivered'
                ORDER BY id DESC
                LIMIT 1
                """,
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}

    def _render_context_block(
        self, row: dict[str, Any], *, header: str = CONTEXT_HEADER
    ) -> str:
        subject = (row.get("subject") or "").strip() or "(no subject)"
        sender = (row.get("sender") or "").strip() or "(unknown sender)"
        received_at = (row.get("received_at") or "").strip() or "(unknown date)"
        body = self._truncate(row.get("cleaned_body") or "")
        parsed_summary = self._summarize_parse(row.get("structured_parse_json"))
        lines = [
            f"--- {header} ---",
            f"email_id: {row['id']}",
            f"subject: {subject}",
            f"from: {sender}",
            f"received_at: {received_at}",
        ]
        if parsed_summary:
            lines.append("structured_summary:")
            lines.append(parsed_summary)
        if body:
            lines.append("body:")
            lines.append(body)
        lines.append(f"--- {CONTEXT_FOOTER} ---")
        return "\n".join(lines)

    def _truncate(self, text: str) -> str:
        text = text.strip()
        if len(text) <= self._max_body_chars:
            return text
        return text[: self._max_body_chars].rstrip() + "\u2026"

    def _summarize_parse(self, parsed_json: str | None) -> str:
        if not parsed_json:
            return ""
        try:
            parsed = json.loads(parsed_json)
        except (TypeError, ValueError):
            return ""
        if not isinstance(parsed, dict):
            return ""
        keep_keys = (
            "email_type",
            "importance",
            "parent_action_required",
            "audience",
            "action_items",
            "calendar_items",
            "telegram_summary",
            "why_it_matters",
        )
        slim = {key: parsed[key] for key in keep_keys if key in parsed}
        try:
            return json.dumps(slim, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            return ""
