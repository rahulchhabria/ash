from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from ash.config import AshConfig
from ash.config.models import ModelConfig
from ash.integrations.close_game_alert import (
    CONTEXT_FOOTER,
    CONTEXT_HEADER,
    CloseGameAlertIntegration,
)
from ash.integrations.runtime import IntegrationContext
from ash.providers.base import IncomingMessage


def _config(*, enabled: bool = True, recent_window_minutes: int = 240) -> AshConfig:
    config = AshConfig(
        workspace=Path("tmp-workspace"),
        models={"default": ModelConfig(provider="openai", model="gpt-5-mini")},
    )
    config.close_game_alert.enabled = enabled
    config.close_game_alert.recent_window_minutes = recent_window_minutes
    return config


def _context(config: AshConfig) -> IntegrationContext:
    return IntegrationContext(
        config=config,
        components=cast(Any, SimpleNamespace()),
        mode="serve",
    )


def _message(
    *, chat_id: str = "c-1", text: str = "what's the score now?"
) -> IncomingMessage:
    return IncomingMessage(
        id="m-1",
        chat_id=chat_id,
        user_id="u-1",
        text=text,
    )


def _seed_history(
    chat_id: str,
    *,
    entries: list[dict[str, Any]],
    ash_home: Path,
) -> None:
    chats_dir = ash_home / "chats" / "telegram" / chat_id
    chats_dir.mkdir(parents=True, exist_ok=True)
    history_path = chats_dir / "history.jsonl"
    with history_path.open("w") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")


def _alert_entry(
    *,
    content: str,
    created_at: datetime,
    role: str = "assistant",
    source: str = "valkyries-close-game-alert",
) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "role": role,
        "content": content,
        "created_at": created_at.isoformat(),
        "metadata": {"source": source},
    }


@pytest.fixture()
def ash_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("ASH_HOME", str(tmp_path))
    from ash.config import paths as paths_module

    paths_module.get_ash_home.cache_clear()
    yield tmp_path
    paths_module.get_ash_home.cache_clear()


@pytest.mark.asyncio
async def test_injects_context_when_recent_alert_present(ash_home: Path) -> None:
    chat_id = "937789129"
    sent_at = datetime.now(UTC) - timedelta(minutes=7)
    alert_text = (
        "Close Game Alert\nValkyries vs Lynx\nGS 80 - 84 MIN\nQ4 1:04\n"
        "Watch: Prime Video"
    )
    _seed_history(
        chat_id,
        entries=[_alert_entry(content=alert_text, created_at=sent_at)],
        ash_home=ash_home,
    )

    integration = CloseGameAlertIntegration()
    context = _context(_config())
    await integration.setup(context)

    message = _message(chat_id=chat_id)
    updated = await integration.preprocess_incoming_message(message, context)

    assert CONTEXT_HEADER in updated.text
    assert CONTEXT_FOOTER in updated.text
    assert "Valkyries vs Lynx" in updated.text
    assert "what's the score now?" in updated.text
    assert "close_game_alert.alert_id" in updated.metadata


@pytest.mark.asyncio
async def test_noop_when_disabled(ash_home: Path) -> None:
    chat_id = "c-disabled"
    _seed_history(
        chat_id,
        entries=[
            _alert_entry(
                content="Close Game Alert\nLakers vs Heat",
                created_at=datetime.now(UTC),
            )
        ],
        ash_home=ash_home,
    )

    integration = CloseGameAlertIntegration()
    context = _context(_config(enabled=False))
    await integration.setup(context)

    message = _message(chat_id=chat_id, text="hi")
    updated = await integration.preprocess_incoming_message(message, context)

    assert updated.text == "hi"
    assert "close_game_alert.alert_id" not in updated.metadata


@pytest.mark.asyncio
async def test_noop_when_no_matching_alert(ash_home: Path) -> None:
    chat_id = "c-no-alert"
    _seed_history(
        chat_id,
        entries=[
            _alert_entry(
                content="Just a friendly assistant reply, not an alert.",
                created_at=datetime.now(UTC),
            )
        ],
        ash_home=ash_home,
    )

    integration = CloseGameAlertIntegration()
    context = _context(_config())
    await integration.setup(context)

    message = _message(chat_id=chat_id, text="hi")
    updated = await integration.preprocess_incoming_message(message, context)

    assert updated.text == "hi"
    assert "close_game_alert.alert_id" not in updated.metadata


@pytest.mark.asyncio
async def test_noop_when_alert_outside_window(ash_home: Path) -> None:
    chat_id = "c-stale"
    sent_at = datetime.now(UTC) - timedelta(hours=12)
    _seed_history(
        chat_id,
        entries=[
            _alert_entry(
                content="Close Game Alert\nValkyries vs Lynx",
                created_at=sent_at,
            )
        ],
        ash_home=ash_home,
    )

    integration = CloseGameAlertIntegration()
    context = _context(_config(recent_window_minutes=60))
    await integration.setup(context)

    message = _message(chat_id=chat_id, text="what's the score now?")
    updated = await integration.preprocess_incoming_message(message, context)

    assert updated.text == "what's the score now?"
    assert "close_game_alert.alert_id" not in updated.metadata


@pytest.mark.asyncio
async def test_picks_most_recent_when_multiple_alerts(ash_home: Path) -> None:
    chat_id = "c-multi"
    older = datetime.now(UTC) - timedelta(minutes=120)
    newer = datetime.now(UTC) - timedelta(minutes=5)
    _seed_history(
        chat_id,
        entries=[
            _alert_entry(
                content="Close Game Alert\nValkyries vs Sky\nGS 70 - 72 CHI",
                created_at=older,
            ),
            _alert_entry(
                content="Close Game Alert\nValkyries vs Lynx\nGS 80 - 84 MIN",
                created_at=newer,
            ),
        ],
        ash_home=ash_home,
    )

    integration = CloseGameAlertIntegration()
    context = _context(_config())
    await integration.setup(context)

    message = _message(chat_id=chat_id)
    updated = await integration.preprocess_incoming_message(message, context)

    assert "Lynx" in updated.text
    assert "Sky" not in updated.text


@pytest.mark.asyncio
async def test_respects_custom_alert_prefixes(ash_home: Path) -> None:
    chat_id = "c-prefix"
    _seed_history(
        chat_id,
        entries=[
            _alert_entry(
                content="Score Watch: Lakers 110 - 112 Heat",
                created_at=datetime.now(UTC),
            )
        ],
        ash_home=ash_home,
    )

    integration = CloseGameAlertIntegration()
    config = _config()
    config.close_game_alert.alert_prefixes = ["Score Watch"]
    context = _context(config)
    await integration.setup(context)

    message = _message(chat_id=chat_id)
    updated = await integration.preprocess_incoming_message(message, context)

    assert "Score Watch" in updated.text
    assert "what's the score now?" in updated.text
