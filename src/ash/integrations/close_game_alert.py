"""Close-game-alert integration contributor.

When a close-game-alert daemon (NBA / WNBA) sends an alert directly to a
Telegram chat, the alert text is delivered to the user via the Telegram
bot but never enters Ash's per-session context. As a result, when the
user replies with a follow-up like ``what's the score now?`` Ash starts
a brand new session with no awareness of the alert.

This contributor closes that gap by inspecting the chat-level history
(``history.jsonl``) for a recent assistant message that looks like a
close-game alert, and prepending a structured context block to the
incoming message so the agent can carry the conversation about the
game (and pick the right close-game-alert skill to fetch live state).

Spec contract: specs/subsystems.md (Integration Hooks),
specs/close_game_alert.md.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ash.chats.history import HistoryEntry, read_recent_chat_history
from ash.integrations.runtime import IntegrationContext, IntegrationContributor

if TYPE_CHECKING:
    from ash.providers.base import IncomingMessage

logger = logging.getLogger("close_game_alert")


CONTEXT_HEADER = "Close-game-alert context (recent alert)"
CONTEXT_FOOTER = "End close-game-alert context"


class CloseGameAlertIntegration(IntegrationContributor):
    """Inject recent close-game-alert context for follow-up questions."""

    name = "close_game_alert"
    priority = 175

    def __init__(self) -> None:
        self._enabled: bool = False
        self._recent_window: timedelta = timedelta(minutes=240)
        self._alert_prefixes: tuple[str, ...] = ("Close Game Alert",)
        self._history_lookback: int = 10

    async def setup(self, context: IntegrationContext) -> None:
        config = context.config.close_game_alert
        if not config.enabled:
            return
        prefixes = tuple(p for p in config.alert_prefixes if p)
        if not prefixes:
            logger.warning(
                "close_game_alert_disabled",
                extra={"reason": "alert_prefixes_empty"},
            )
            return
        self._alert_prefixes = prefixes
        self._recent_window = timedelta(minutes=config.recent_window_minutes)
        self._history_lookback = config.history_lookback
        self._enabled = True
        logger.info(
            "close_game_alert_ready",
            extra={
                "close_game_alert.recent_window_minutes": (
                    config.recent_window_minutes
                ),
                "close_game_alert.alert_prefixes": list(prefixes),
            },
        )

    async def preprocess_incoming_message(
        self,
        message: IncomingMessage,
        context: IntegrationContext,
    ) -> IncomingMessage:
        if not self._enabled:
            return message

        provider = message.metadata.get("provider_name") or "telegram"
        chat_id = message.chat_id
        if not chat_id:
            return message

        try:
            entries = read_recent_chat_history(
                provider, chat_id, limit=self._history_lookback
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "close_game_alert_history_read_failed",
                extra={"error.message": str(exc)},
            )
            return message

        recent_alert = self._latest_alert(entries, now=self._now())
        if recent_alert is None:
            return message

        context_block = self._render_context_block(recent_alert)
        prefixed = f"{context_block}\n\n{message.text}".strip()
        message.text = prefixed
        message.metadata = {
            **message.metadata,
            "close_game_alert.alert_id": recent_alert.id,
            "close_game_alert.alert_created_at": (recent_alert.created_at.isoformat()),
        }
        logger.info(
            "close_game_alert_context_injected",
            extra={
                "close_game_alert.alert_id": recent_alert.id,
                "close_game_alert.age_minutes": int(
                    (self._now() - recent_alert.created_at).total_seconds() / 60
                ),
            },
        )
        return message

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def _latest_alert(
        self,
        entries: list[HistoryEntry],
        *,
        now: datetime,
    ) -> HistoryEntry | None:
        cutoff = now - self._recent_window
        for entry in reversed(entries):
            if entry.role != "assistant":
                continue
            if not self._matches_alert(entry.content):
                continue
            created = entry.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            if created < cutoff:
                continue
            return entry
        return None

    def _matches_alert(self, content: str) -> bool:
        head = content.lstrip()
        return any(head.startswith(prefix) for prefix in self._alert_prefixes)

    def _render_context_block(self, entry: HistoryEntry) -> str:
        created = entry.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        timestamp = created.isoformat()
        source = ""
        if entry.metadata:
            source = str(entry.metadata.get("source") or "")
        lines = [
            f"--- {CONTEXT_HEADER} ---",
            f"sent_at_utc: {timestamp}",
        ]
        if source:
            lines.append(f"source: {source}")
        lines.append("alert_message:")
        lines.append(entry.content.strip())
        lines.append(
            "guidance: Treat this as the conversation focus when the user "
            "asks about the score, the game, or the alert. If the user "
            "asks for current state, use the matching close-game-alert "
            "skill (e.g. valkyries-close-game-alert / close-game-alert) "
            "to fetch live data; do not assume the alert numbers are "
            "still current."
        )
        lines.append(f"--- {CONTEXT_FOOTER} ---")
        return "\n".join(lines)
