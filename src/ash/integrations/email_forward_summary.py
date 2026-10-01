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
import re
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ash.chats import ChatStateManager
from ash.chats.history import ChatHistoryWriter, find_chat_history_message
from ash.context_firewall import check_context_injection
from ash.integrations.runtime import IntegrationContext, IntegrationContributor

if TYPE_CHECKING:
    from ash.providers.base import IncomingMessage

logger = logging.getLogger("email_forward_summary")


CONTEXT_HEADER = "Email-forward-summary context (reply target)"
ACTIVE_FOCUS_CONTEXT_HEADER = "Email-forward-summary context (active focus)"
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

        # Spec: specs/email_forward_summary.md. Recover pre-fix deliveries using
        # chat-scoped provenance, never by searching another chat's email IDs.
        self._recover_delivered_history(message.chat_id)
        if re.match(
            r"^\s*(new topic|new thread|start over)\b", message.text or "", re.I
        ):
            return message

        row: dict[str, Any] | None = None
        source = "reply"
        header = CONTEXT_HEADER
        reply_to = message.reply_to_message_id

        if reply_to:
            try:
                tg_message_id = int(reply_to)
            except (TypeError, ValueError):
                return message

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
                return message
        if row is None:
            row = self._lookup_thread_source(message)
            if row is not None:
                source = "thread"
                header = "Email-forward-summary context (conversation source)"

        if row is None and not reply_to:
            focus = self._select_active_email_focus(message)
            if focus is None:
                return message
            email_id = self._email_id_from_source_id(focus.source_id)
            if email_id is None:
                return message
            try:
                row = self._lookup_email_by_id(email_id)
            except sqlite3.Error as exc:
                logger.warning(
                    "email_forward_summary_lookup_failed",
                    extra={
                        "error.message": str(exc),
                        "email_forward_summary.email_id": email_id,
                        "email_forward_summary.source": "active_focus",
                    },
                )
                return message
            source = "active_focus"
            header = ACTIVE_FOCUS_CONTEXT_HEADER

        if row is None:
            return message

        decision = check_context_injection(
            context.config,
            integration=self.name,
            trigger="reply" if source == "reply" else "explicit",
            message=message,
        )
        if not decision.allowed:
            return message

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

    def _recover_delivered_history(self, chat_id: str) -> None:
        """Backfill retained legacy focus summaries once, with original timestamps."""
        state = ChatStateManager(provider="telegram", chat_id=chat_id).load()
        writer = ChatHistoryWriter(provider="telegram", chat_id=chat_id)
        for focus in state.active_focus:
            if focus.kind != "email" or not focus.telegram_message_id:
                continue
            if find_chat_history_message(
                "telegram", chat_id, focus.telegram_message_id
            ):
                continue
            writer.record_bot_message(
                content=focus.summary or focus.title,
                created_at=focus.created_at,
                metadata={
                    "external_id": focus.telegram_message_id,
                    "thread_id": focus.thread_id or focus.telegram_message_id,
                    "source_id": focus.source_id,
                    "source": self.name,
                    "recovered_summary": True,
                },
            )

    def _lookup_thread_source(self, message: IncomingMessage) -> dict[str, Any] | None:
        """Recover the source of the resolved conversation without lexical guessing."""
        state = ChatStateManager(provider="telegram", chat_id=message.chat_id).load()
        thread_id = message.metadata.get("thread_id")
        if not thread_id and message.reply_to_message_id:
            thread_id = state.thread_index.get(message.reply_to_message_id)
        if not thread_id and not message.reply_to_message_id:
            thread_id = state.get_active_thread()
        if not thread_id:
            return None
        entry = find_chat_history_message("telegram", message.chat_id, str(thread_id))
        metadata = entry.metadata if entry else None
        if not metadata or metadata.get("source") != self.name:
            return None
        email_id = self._email_id_from_source_id(str(metadata.get("source_id", "")))
        if email_id is None:
            return None
        try:
            return self._lookup_email_by_id(email_id)
        except sqlite3.Error as exc:
            logger.warning(
                "email_forward_summary_lookup_failed", extra={"error.message": str(exc)}
            )
            return None

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

    def _lookup_email_by_id(self, email_id: int) -> dict[str, Any] | None:
        assert self._db_path is not None
        with sqlite3.connect(f"file:{self._db_path}?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                """
                SELECT id, subject, sender, received_at, cleaned_body,
                       structured_parse_json, processing_status
                FROM emails
                WHERE id = ?
                  AND processing_status = 'delivered'
                ORDER BY id DESC
                LIMIT 1
                """,
                (email_id,),
            )
            row = cursor.fetchone()
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}

    def _select_active_email_focus(self, message: IncomingMessage) -> Any | None:
        text = (message.text or "").strip()
        if not _looks_like_contextual_followup(text):
            return None

        manager = ChatStateManager(provider="telegram", chat_id=message.chat_id)
        state = manager.load()
        matches = []
        for focus in state.get_recent_focus(kind="email"):
            if _focus_matches_text(focus, text):
                matches.append(focus)

        if len(matches) != 1:
            return None

        manager.save()
        return matches[0]

    @staticmethod
    def _email_id_from_source_id(source_id: str) -> int | None:
        if not source_id.startswith("email:"):
            return None
        try:
            return int(source_id.split(":", 1)[1])
        except ValueError:
            return None

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


_CONTEXTUAL_START_RE = re.compile(
    r"^\s*(where|when|what|who|which|how|is|are|do|does|did|can|could|should|"
    r"need|remind|tell me|what about)\b",
    re.I,
)
_CONTEXTUAL_PRONOUN_RE = re.compile(
    r"\b(it|that|this|there|they|them|those|one)\b", re.I
)
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'-]*", re.I)
_STOPWORDS = {
    "about",
    "after",
    "again",
    "also",
    "and",
    "are",
    "can",
    "could",
    "did",
    "do",
    "does",
    "for",
    "from",
    "how",
    "is",
    "it",
    "me",
    "need",
    "of",
    "on",
    "or",
    "should",
    "tell",
    "that",
    "the",
    "them",
    "there",
    "they",
    "this",
    "to",
    "was",
    "what",
    "when",
    "where",
    "which",
    "who",
    "with",
    "you",
    "i",
    "my",
    "we",
    "our",
}


def _looks_like_contextual_followup(text: str) -> bool:
    if not text:
        return False
    words = _content_words(text)
    return bool(
        _CONTEXTUAL_START_RE.search(text)
        or _CONTEXTUAL_PRONOUN_RE.search(text)
        or len(words) <= 4
    )


def _focus_matches_text(focus: Any, text: str) -> bool:
    words = set(_content_words(text))
    focus_terms = set()
    for value in [focus.title, focus.summary or "", *focus.entities]:
        focus_terms.update(_content_words(value))

    if words & focus_terms:
        return True
    return bool(_CONTEXTUAL_PRONOUN_RE.search(text)) and len(words) <= 5


def _content_words(text: str) -> list[str]:
    return [
        word.lower()
        for word in _WORD_RE.findall(text or "")
        if len(word) > 1 and word.lower() not in _STOPWORDS
    ]
