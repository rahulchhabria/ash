"""Small JSONL event router for sanitized external signals."""

from __future__ import annotations

import json
import os
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ash.config.paths import get_ash_home

MAX_EVENT_SOURCE_CHARS = 100
MAX_EVENT_KIND_CHARS = 100
MAX_EVENT_TITLE_CHARS = 300
MAX_EVENT_BODY_CHARS = 8000
MAX_EVENT_METADATA_BYTES = 50_000


@dataclass(frozen=True, slots=True)
class RoutedEvent:
    id: str
    ts: str
    source: str
    kind: str
    title: str
    body: str
    metadata: dict[str, Any]


def event_log_path(path: Path | None = None) -> Path:
    return path or get_ash_home() / "events" / "events.jsonl"


def validate_event_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Return a JSON-safe metadata copy after enforcing a serialized size cap."""
    value = metadata or {}
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("event metadata must be valid JSON") from exc
    if len(encoded.encode("utf-8")) > MAX_EVENT_METADATA_BYTES:
        raise ValueError("event metadata exceeds 50000 bytes")
    return json.loads(encoded)


def record_event(
    *,
    source: str,
    kind: str,
    title: str,
    body: str = "",
    metadata: dict[str, Any] | None = None,
    path: Path | None = None,
) -> RoutedEvent:
    event = RoutedEvent(
        id=f"evt_{uuid.uuid4().hex[:16]}",
        ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        source=(source.strip() or "unknown")[:MAX_EVENT_SOURCE_CHARS],
        kind=(kind.strip() or "event")[:MAX_EVENT_KIND_CHARS],
        title=title.strip()[:MAX_EVENT_TITLE_CHARS],
        body=body.strip()[:MAX_EVENT_BODY_CHARS],
        metadata=validate_event_metadata(metadata),
    )
    target = event_log_path(path)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.parent.chmod(0o700)
    encoded_event = (json.dumps(asdict(event), sort_keys=True) + "\n").encode()
    fd = os.open(target, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, encoded_event)
    finally:
        os.close(fd)
    return event


def event_to_dict(event: RoutedEvent) -> dict[str, Any]:
    return asdict(event)


def read_events(*, limit: int = 50, path: Path | None = None) -> list[dict[str, Any]]:
    bounded_limit = max(0, min(int(limit), 200))
    if bounded_limit == 0:
        return []
    target = event_log_path(path)
    if not target.exists():
        return []
    lines: deque[str] = deque(maxlen=max(bounded_limit * 2, bounded_limit))
    with target.open(encoding="utf-8") as fh:
        for line in fh:
            lines.append(line)
    events: list[dict[str, Any]] = []
    for line in reversed(lines):
        try:
            raw = json.loads(line)
        except ValueError:
            continue
        if isinstance(raw, dict):
            events.append(raw)
        if len(events) >= bounded_limit:
            break
    return events
