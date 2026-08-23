from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from ash.config import AshConfig, load_config
from ash.config.models import ModelConfig
from ash.integrations.vapi import parse_vapi_webhook
from ash.server.routes import vapi

FIXTURE = Path(__file__).parent / "fixtures" / "vapi_end_of_call_report.json"


def _payload() -> dict:
    return json.loads(FIXTURE.read_text())


def _config(*, secret: str | None = None) -> AshConfig:
    config = AshConfig(
        workspace=Path("tmp-workspace"),
        models={"default": ModelConfig(provider="openai", model="gpt-5-mini")},
    )
    config.vapi.enabled = True
    config.vapi.telegram_chat_id = "12345"
    config.vapi.telegram_user_id = "67890"
    if secret:
        config.vapi.webhook_secret = SecretStr(secret)
    return config


def test_parse_vapi_end_of_call_report_builds_normal_incoming_message() -> None:
    result = parse_vapi_webhook(_payload(), config=_config().vapi)

    assert result.ignored is False
    assert result.message is not None
    assert result.message.id == "vapi:call_abc123"
    assert result.message.chat_id == "12345"
    assert result.message.user_id == "67890"
    assert result.message.metadata["source"] == "vapi_voicemail"
    assert result.message.metadata["vapi.caller_number"] == "+14155551234"
    assert result.message.metadata["vapi.duration_seconds"] == 42
    assert "Source: vapi_voicemail" in result.message.text
    assert "Hey Rahul, it's John" in result.message.text


def test_parse_vapi_ignores_unrelated_events() -> None:
    result = parse_vapi_webhook(
        {"message": {"type": "status-update", "status": "ended"}},
        config=_config().vapi,
    )

    assert result.ignored is True
    assert result.reason == "unsupported_message_type"
    assert result.message is None


def test_vapi_route_submits_to_existing_telegram_handler() -> None:
    app = FastAPI()
    app.include_router(vapi.router)
    handler = SimpleNamespace(handle_message=AsyncMock())
    app.state.config = _config(secret="test-secret")
    app.state.server = SimpleNamespace(
        get_telegram_handler=AsyncMock(return_value=handler)
    )

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/vapi",
            json=_payload(),
            headers={"Authorization": "Bearer test-secret"},
        )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}
    handler.handle_message.assert_awaited_once()
    message = handler.handle_message.await_args.args[0]
    assert message.id == "vapi:call_abc123"
    assert message.metadata["source"] == "vapi_voicemail"


def test_vapi_route_rejects_invalid_secret() -> None:
    app = FastAPI()
    app.include_router(vapi.router)
    app.state.config = _config(secret="test-secret")
    app.state.server = SimpleNamespace(get_telegram_handler=AsyncMock())

    with TestClient(app) as client:
        response = client.post(
            "/webhooks/vapi",
            json=_payload(),
            headers={"Authorization": "Bearer wrong"},
        )

    assert response.status_code == 401


def test_vapi_webhook_secret_loads_from_env(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        """
workspace = "tmp-workspace"

[models.default]
provider = "openai"
model = "gpt-5-mini"

[vapi]
enabled = true
telegram_chat_id = "12345"
""".strip()
    )
    monkeypatch.setenv("VAPI_WEBHOOK_SECRET", "env-secret")

    config = load_config(config_path)

    assert config.vapi.webhook_secret is not None
    assert config.vapi.webhook_secret.get_secret_value() == "env-secret"
