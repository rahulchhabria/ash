"""Policy checks for integration-supplied context blocks."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from ash.config import AshConfig
    from ash.providers.base import IncomingMessage

logger = logging.getLogger("context_firewall")

Trigger = Literal["reply", "explicit", "classifier", "recent", "ambient"]


@dataclass(frozen=True, slots=True)
class ContextDecision:
    allowed: bool
    reason: str


def check_context_injection(
    config: AshConfig,
    *,
    integration: str,
    trigger: Trigger,
    message: IncomingMessage,
) -> ContextDecision:
    """Return whether an integration may inject context into a user turn."""
    firewall = config.context_firewall
    if not firewall.enabled:
        return ContextDecision(True, "firewall_disabled")

    if integration in firewall.blocked_integrations:
        _log_decision(integration, trigger, message, False, "integration_blocked")
        return ContextDecision(False, "integration_blocked")

    if firewall.allowed_integrations and integration not in firewall.allowed_integrations:
        _log_decision(integration, trigger, message, False, "integration_not_allowed")
        return ContextDecision(False, "integration_not_allowed")

    if trigger not in firewall.allowed_triggers:
        _log_decision(integration, trigger, message, False, "trigger_not_allowed")
        return ContextDecision(False, "trigger_not_allowed")

    if trigger == "reply" and not message.reply_to_message_id:
        _log_decision(integration, trigger, message, False, "reply_trigger_without_reply")
        return ContextDecision(False, "reply_trigger_without_reply")

    _log_decision(integration, trigger, message, True, "allowed")
    return ContextDecision(True, "allowed")


def _log_decision(
    integration: str,
    trigger: Trigger,
    message: IncomingMessage,
    allowed: bool,
    reason: str,
) -> None:
    logger.info(
        "context_firewall_decision",
        extra={
            "context_firewall.integration": integration,
            "context_firewall.trigger": trigger,
            "context_firewall.allowed": allowed,
            "context_firewall.reason": reason,
            "chat_id": message.chat_id,
            "user_id": message.user_id,
            "thread_id": message.metadata.get("thread_id"),
        },
    )
