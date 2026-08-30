from pathlib import Path

from ash.config import AshConfig
from ash.config.models import ModelConfig
from ash.context_firewall import check_context_injection
from ash.providers.base import IncomingMessage


def _config() -> AshConfig:
    return AshConfig(
        workspace=Path("tmp-workspace"),
        models={"default": ModelConfig(provider="openai", model="gpt-5-mini")},
    )


def _message(reply_to: str | None = None) -> IncomingMessage:
    return IncomingMessage(
        id="m-1",
        chat_id="c-1",
        user_id="u-1",
        text="hello",
        reply_to_message_id=reply_to,
    )


def test_allows_reply_context_by_default() -> None:
    decision = check_context_injection(
        _config(),
        integration="email_forward_summary",
        trigger="reply",
        message=_message("42"),
    )
    assert decision.allowed is True


def test_blocks_ambient_context_by_default() -> None:
    decision = check_context_injection(
        _config(),
        integration="email_forward_summary",
        trigger="ambient",
        message=_message(),
    )
    assert decision.allowed is False
    assert decision.reason == "trigger_not_allowed"


def test_blocks_named_integration() -> None:
    config = _config()
    config.context_firewall.blocked_integrations = ["close_game_alert"]
    decision = check_context_injection(
        config,
        integration="close_game_alert",
        trigger="explicit",
        message=_message(),
    )
    assert decision.allowed is False
    assert decision.reason == "integration_blocked"
