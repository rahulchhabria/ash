import stat

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from ash.config import AshConfig
from ash.config.models import ModelConfig
from ash.events import MAX_EVENT_METADATA_BYTES, read_events, record_event
from ash.server.routes import control


def test_record_and_read_events(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    event = record_event(
        source="test",
        kind="alert",
        title="Hello",
        body="World",
        metadata={"x": 1},
        path=path,
    )

    events = read_events(limit=5, path=path)

    assert event.id.startswith("evt_")
    assert events[0]["title"] == "Hello"
    assert events[0]["metadata"] == {"x": 1}


def test_record_event_caps_fields_and_uses_private_permissions(tmp_path) -> None:
    path = tmp_path / "events" / "events.jsonl"
    event = record_event(
        source="s" * 200,
        kind="k" * 200,
        title="t" * 500,
        body="b" * 10_000,
        metadata={"x": 1},
        path=path,
    )

    assert len(event.source) == 100
    assert len(event.kind) == 100
    assert len(event.title) == 300
    assert len(event.body) == 8000
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_record_event_rejects_oversized_metadata(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    with pytest.raises(ValueError, match="metadata exceeds"):
        record_event(
            source="test",
            kind="alert",
            title="large",
            metadata={"blob": "x" * MAX_EVENT_METADATA_BYTES},
            path=path,
        )


def _event_app() -> tuple[FastAPI, AshConfig]:
    app = FastAPI()
    app.include_router(control.router)
    config = AshConfig(
        workspace="tmp-workspace",
        models={"default": ModelConfig(provider="openai", model="gpt-5-mini")},
    )
    app.state.config = config
    app.state.integration_runtime = None
    app.state.skill_registry = None
    return app, config


def test_event_intake_fails_closed_without_required_token() -> None:
    app, _ = _event_app()

    with TestClient(app) as client:
        response = client.post("/events", json={"title": "test"})

    assert response.status_code == 503


def test_event_intake_accepts_configured_bearer_token(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        control,
        "record_event",
        lambda **kwargs: record_event(path=tmp_path / "events.jsonl", **kwargs),
    )
    app, config = _event_app()
    config.event_router.bearer_token = SecretStr("test-token")

    with TestClient(app) as client:
        rejected = client.post("/events", json={"title": "test"})
        accepted = client.post(
            "/events",
            json={"title": "test"},
            headers={"Authorization": "Bearer test-token"},
        )

    assert rejected.status_code == 401
    assert accepted.status_code == 200


def test_event_reads_and_dashboard_require_auth(monkeypatch) -> None:
    monkeypatch.setattr(control, "read_events", lambda **_: [{"title": "safe"}])
    app, config = _event_app()
    config.event_router.bearer_token = SecretStr("test-token")

    with TestClient(app) as client:
        protected_paths = (
            "/events",
            "/dashboard",
            "/dashboard/status",
            "/dashboard/skills",
        )
        for path in protected_paths:
            response = client.get(path)
            assert response.status_code == 401

        accepted = client.get(
            "/events",
            headers={"Authorization": "Bearer test-token"},
        )
        basic = client.get("/dashboard/status", auth=("ash", "test-token"))

    assert accepted.status_code == 200
    assert accepted.json() == [{"title": "safe"}]
    assert basic.status_code == 200


def test_event_intake_can_explicitly_disable_auth(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        control,
        "record_event",
        lambda **kwargs: record_event(path=tmp_path / "events.jsonl", **kwargs),
    )
    app, config = _event_app()
    config.event_router.auth_required = False

    with TestClient(app) as client:
        response = client.post("/events", json={"title": "test"})

    assert response.status_code == 200


def test_event_intake_rejects_oversized_metadata() -> None:
    app, config = _event_app()
    config.event_router.bearer_token = SecretStr("test-token")

    with TestClient(app) as client:
        response = client.post(
            "/events",
            json={"title": "test", "metadata": {"blob": "x" * 50_000}},
            headers={"Authorization": "Bearer test-token"},
        )

    assert response.status_code == 422
