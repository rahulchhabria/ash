"""Observability module for Sentry integration."""

import importlib
import logging
import re
from typing import TYPE_CHECKING, Any

__all__ = ["init_sentry", "set_sentry_conversation_id"]

if TYPE_CHECKING:
    from ash.config import SentryConfig

logger = logging.getLogger(__name__)

_REDACTED = "[Filtered]"
_SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "bot_token",
    "cookie",
    "password",
    "secret",
    "set-cookie",
    "token",
}
_TELEGRAM_BOT_URL = re.compile(
    r"(https?://api\.telegram\.org/bot)[^/\s?#]+",
    flags=re.IGNORECASE,
)
_BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_TELEGRAM_POLL_DISCONNECT_MARKERS = (
    "Failed to fetch updates",
    "TelegramNetworkError",
    "ServerDisconnectedError",
    "Server disconnected",
)
_PLAYWRIGHT_SCREENSHOT_TIMEOUT_MARKERS = (
    "Future exception was never retrieved",
    "Timeout",
    "taking page screenshot",
)


def _scrub_sentry_value(value: Any, *, key: str | None = None) -> Any:
    """Remove credentials from nested Sentry event, breadcrumb, and log data."""
    if key and key.lower() in _SENSITIVE_KEYS:
        return _REDACTED
    if isinstance(value, dict):
        return {
            item_key: _scrub_sentry_value(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_scrub_sentry_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub_sentry_value(item) for item in value)
    if isinstance(value, str):
        value = _TELEGRAM_BOT_URL.sub(r"\1[Filtered]", value)
        return _BEARER_TOKEN.sub("Bearer [Filtered]", value)
    return value


def _payload_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(_payload_text(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(_payload_text(item) for item in value)
    return ""


def _payload_has_logger(payload: dict[str, Any], expected: str) -> bool:
    if str(payload.get("logger") or "") == expected:
        return True
    tags = payload.get("tags")
    if isinstance(tags, dict):
        return str(tags.get("logger") or "") == expected
    if isinstance(tags, list):
        return any(
            isinstance(tag, (list, tuple))
            and len(tag) >= 2
            and str(tag[0]) == "logger"
            and str(tag[1]) == expected
            for tag in tags
        )
    return False


def _is_telegram_poll_disconnect(payload: dict[str, Any]) -> bool:
    candidate_text = _payload_text(payload)
    return _payload_has_logger(payload, "aiogram.dispatcher") and all(
        marker in candidate_text for marker in _TELEGRAM_POLL_DISCONNECT_MARKERS
    )


def _is_playwright_screenshot_future_timeout(payload: dict[str, Any]) -> bool:
    candidate_text = _payload_text(payload)
    return _payload_has_logger(payload, "asyncio") and all(
        marker in candidate_text for marker in _PLAYWRIGHT_SCREENSHOT_TIMEOUT_MARKERS
    )


def _should_drop_sentry_payload(payload: dict[str, Any]) -> bool:
    return _is_telegram_poll_disconnect(
        payload
    ) or _is_playwright_screenshot_future_timeout(payload)


def _before_send(event: Any, _hint: Any) -> Any:
    if _should_drop_sentry_payload(event):
        return None
    return _scrub_sentry_value(event)


def _before_breadcrumb(
    breadcrumb: dict[str, Any], _hint: dict[str, Any]
) -> dict[str, Any]:
    return _scrub_sentry_value(breadcrumb)


def _before_send_log(log: Any, _hint: Any) -> Any:
    if _should_drop_sentry_payload(log):
        return None
    return _scrub_sentry_value(log)


try:
    import sentry_sdk
    from sentry_sdk.integrations.asyncio import (
        AsyncioIntegration,
    )
    from sentry_sdk.integrations.logging import (
        LoggingIntegration,
    )

    SENTRY_AVAILABLE = True
except ImportError:
    SENTRY_AVAILABLE = False


def init_sentry(config: "SentryConfig", server_mode: bool = False) -> bool:
    """Initialize Sentry if configured.

    Returns True if Sentry was initialized, False otherwise.
    Server mode enables FastAPI integration.
    """
    if not SENTRY_AVAILABLE:
        logger.debug("Sentry SDK not installed, skipping initialization")
        return False

    if not config.dsn or not config.dsn.get_secret_value():
        logger.debug("Sentry DSN not configured, skipping initialization")
        return False

    integrations: list[Any] = [
        AsyncioIntegration(),
        LoggingIntegration(
            level=logging.INFO,  # Capture INFO+ as breadcrumbs
            event_level=logging.ERROR,  # Create events for ERROR+
            sentry_logs_level=logging.INFO,  # Send INFO+ emitted app logs to Sentry Logs
        ),
    ]

    if server_mode:
        from sentry_sdk.integrations.fastapi import (
            FastApiIntegration,
        )

        integrations.append(FastApiIntegration())

    sentry_sdk.init(
        dsn=config.dsn.get_secret_value(),
        environment=config.environment,
        release=config.release,
        traces_sample_rate=config.traces_sample_rate,
        profiles_sample_rate=config.profiles_sample_rate,
        stream_gen_ai_spans=config.stream_gen_ai_spans,
        send_default_pii=config.send_default_pii,
        debug=config.debug,
        enable_logs=True,
        before_send=_before_send,
        before_breadcrumb=_before_breadcrumb,
        before_send_log=_before_send_log,
        integrations=integrations,
    )

    logger.info("sentry_initialized", extra={"sentry.environment": config.environment})
    return True


def set_sentry_conversation_id(conversation_id: str | None) -> None:
    """Set the active Sentry AI conversation ID when the SDK is available."""
    if not SENTRY_AVAILABLE or not conversation_id:
        return

    try:
        sentry_ai = importlib.import_module("sentry_sdk.ai")
        sentry_ai.set_conversation_id(conversation_id)
    except Exception:
        logger.debug("sentry_conversation_id_skipped", exc_info=True)
