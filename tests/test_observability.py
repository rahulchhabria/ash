from __future__ import annotations

import logging

from pydantic import SecretStr

from ash.config.models import SentryConfig


def test_init_sentry_enables_sentry_logs(monkeypatch) -> None:
    from ash import observability

    calls: dict[str, object] = {}

    class FakeLoggingIntegration:
        def __init__(self, **kwargs) -> None:  # noqa: ANN001
            calls["logging_integration"] = kwargs

    class FakeAsyncioIntegration:
        pass

    def fake_init(**kwargs) -> None:  # noqa: ANN001
        calls["init"] = kwargs

    monkeypatch.setattr(observability, "SENTRY_AVAILABLE", True)
    monkeypatch.setattr(observability, "LoggingIntegration", FakeLoggingIntegration)
    monkeypatch.setattr(observability, "AsyncioIntegration", FakeAsyncioIntegration)
    monkeypatch.setattr(observability.sentry_sdk, "init", fake_init)

    initialized = observability.init_sentry(
        SentryConfig(
            dsn=SecretStr("https://public@example.com/1"),
            stream_gen_ai_spans=True,
        )
    )

    assert initialized is True
    assert calls["logging_integration"] == {
        "level": logging.INFO,
        "event_level": logging.ERROR,
        "sentry_logs_level": logging.DEBUG,
    }
    assert calls["init"]["enable_logs"] is True
    assert calls["init"]["stream_gen_ai_spans"] is True


def test_set_sentry_conversation_id(monkeypatch) -> None:
    from ash import observability

    calls: dict[str, str] = {}

    class FakeAI:
        @staticmethod
        def set_conversation_id(conversation_id: str) -> None:
            calls["conversation_id"] = conversation_id

    monkeypatch.setattr(observability, "SENTRY_AVAILABLE", True)
    monkeypatch.setattr(
        observability.importlib,
        "import_module",
        lambda name: FakeAI if name == "sentry_sdk.ai" else None,
    )

    observability.set_sentry_conversation_id("chat-session-123")

    assert calls["conversation_id"] == "chat-session-123"
