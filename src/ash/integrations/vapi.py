"""Vapi voicemail webhook adapter.

Converts Vapi end-of-call reports into normal Ash provider messages.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ash.config.models import VapiConfig
from ash.providers.base import IncomingMessage

VOICEMAIL_SOURCE = "vapi_voicemail"


@dataclass(frozen=True, slots=True)
class VapiWebhookResult:
    """Parsed Vapi webhook outcome."""

    ignored: bool
    message: IncomingMessage | None = None
    reason: str | None = None


def parse_vapi_webhook(
    payload: dict[str, Any],
    *,
    config: VapiConfig,
) -> VapiWebhookResult:
    """Parse a Vapi webhook payload into an Ash incoming message."""
    raw_message = payload.get("message")
    if not isinstance(raw_message, dict):
        return VapiWebhookResult(ignored=True, reason="missing_message")

    message_type = _string(raw_message.get("type"))
    if message_type != "end-of-call-report":
        return VapiWebhookResult(ignored=True, reason="unsupported_message_type")

    chat_id = _string(config.telegram_chat_id)
    if not chat_id:
        raise ValueError("vapi.telegram_chat_id is required")
    user_id = _string(config.telegram_user_id) or chat_id

    metadata = _extract_metadata(raw_message)
    external_id = _external_id(raw_message, metadata)
    text = _render_voicemail_message(metadata)

    return VapiWebhookResult(
        ignored=False,
        message=IncomingMessage(
            id=external_id,
            chat_id=chat_id,
            user_id=user_id,
            text=text,
            username=VOICEMAIL_SOURCE,
            display_name="Vapi voicemail",
            metadata={
                "source": VOICEMAIL_SOURCE,
                "processing_mode": "active",
                "chat_type": "private",
                **{f"vapi.{key}": value for key, value in metadata.items()},
            },
            timestamp=_parse_timestamp(metadata.get("timestamp")),
        ),
    )


def _extract_metadata(message: dict[str, Any]) -> dict[str, Any]:
    call = _dict(message.get("call"))
    artifact = _dict(message.get("artifact"))
    recording = _dict(artifact.get("recording"))
    customer = _dict(call.get("customer"))
    phone_number = _dict(call.get("phoneNumber"))

    started_at = _string(message.get("startedAt")) or _string(call.get("startedAt"))
    ended_at = _string(message.get("endedAt")) or _string(call.get("endedAt"))
    duration_seconds = _duration_seconds(
        message, started_at=started_at, ended_at=ended_at
    )
    recording_url = (
        _string(message.get("recordingUrl"))
        or _string(recording.get("url"))
        or _string(recording.get("monoUrl"))
        or _string(recording.get("stereoUrl"))
        or _string(message.get("stereoRecordingUrl"))
    )

    metadata: dict[str, Any] = {
        "message_type": "end-of-call-report",
        "call_id": _string(call.get("id")) or _string(message.get("callId")),
        "event_id": _string(message.get("id")) or _string(message.get("eventId")),
        "caller_number": (
            _string(customer.get("number"))
            or _string(call.get("customerNumber"))
            or _string(call.get("from"))
            or _string(message.get("customerNumber"))
        ),
        "phone_number": (
            _string(phone_number.get("number"))
            or _string(call.get("phoneNumber"))
            or _string(call.get("to"))
        ),
        "duration_seconds": duration_seconds,
        "recording_url": recording_url,
        "timestamp": ended_at or started_at or _string(message.get("timestamp")),
        "started_at": started_at,
        "ended_at": ended_at,
        "ended_reason": _string(message.get("endedReason")),
        "summary": _string(_dict(message.get("analysis")).get("summary")),
        "transcript": (
            _string(message.get("transcript")) or _string(artifact.get("transcript"))
        ),
    }

    return {key: value for key, value in metadata.items() if value not in (None, "")}


def _render_voicemail_message(metadata: dict[str, Any]) -> str:
    lines = [
        "You received a new voicemail for Rahul. Summarize it clearly and send "
        "Rahul a Telegram message. Preserve important names, dates, numbers, "
        "requests, and commitments. If the caller is asking Rahul to do something, "
        "make the requested action obvious. Include the caller number and recording "
        "link when available.",
        "",
        "Source: vapi_voicemail",
    ]
    field_labels = (
        ("caller_number", "Caller"),
        ("duration_seconds", "Duration"),
        ("recording_url", "Recording"),
        ("call_id", "Call ID"),
        ("timestamp", "Timestamp"),
        ("ended_reason", "Ended reason"),
        ("summary", "Vapi summary"),
    )
    for key, label in field_labels:
        value = metadata.get(key)
        if value is None:
            continue
        rendered = f"{value} seconds" if key == "duration_seconds" else str(value)
        lines.append(f"{label}: {rendered}")

    transcript = str(metadata.get("transcript") or "").strip()
    lines.extend(
        ["", "Voicemail transcript:", transcript or "(no transcript provided)"]
    )
    return "\n".join(lines).strip()


def _external_id(message: dict[str, Any], metadata: dict[str, Any]) -> str:
    stable = _string(metadata.get("event_id")) or _string(metadata.get("call_id"))
    if stable:
        return f"vapi:{stable}"
    canonical = json.dumps(message, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return f"vapi:payload:{digest}"


def _duration_seconds(
    message: dict[str, Any],
    *,
    started_at: str | None,
    ended_at: str | None,
) -> int | None:
    raw = (
        message.get("durationSeconds")
        or message.get("duration")
        or _dict(message.get("call")).get("durationSeconds")
    )
    if isinstance(raw, int | float):
        return max(0, int(raw))
    if isinstance(raw, str):
        try:
            return max(0, int(float(raw)))
        except ValueError:
            pass

    start = _parse_timestamp(started_at)
    end = _parse_timestamp(ended_at)
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds()))


def _parse_timestamp(value: Any) -> datetime | None:
    text = _string(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return str(value)
