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
        "sentry_logs_level": logging.INFO,
    }
    assert calls["init"]["enable_logs"] is True
    assert calls["init"]["stream_gen_ai_spans"] is True
    assert calls["init"]["before_send"] is observability._before_send
    assert calls["init"]["before_breadcrumb"] is observability._before_breadcrumb
    assert calls["init"]["before_send_log"] is observability._before_send_log


def test_sentry_processors_scrub_nested_credentials() -> None:
    from ash import observability

    token = "123456:secret-value"
    payload = {
        "request": {
            "url": f"https://api.telegram.org/bot{token}/getMe",
            "headers": {
                "Authorization": "Bearer another-secret",
                "Cookie": "session=secret",
            },
        },
        "extra": {
            "api_key": "key-value",
            "items": [f"POST https://api.telegram.org/bot{token}/getUpdates"],
        },
    }

    scrubbed = observability._before_send(payload, {})

    rendered = repr(scrubbed)
    assert token not in rendered
    assert "another-secret" not in rendered
    assert "session=secret" not in rendered
    assert "key-value" not in rendered
    assert rendered.count("[Filtered]") >= 4


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
