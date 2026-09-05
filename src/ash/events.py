"""Small JSONL event router for sanitized external signals."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ash.config.paths import get_ash_home


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
        source=source.strip() or "unknown",
        kind=kind.strip() or "event",
        title=title.strip()[:300],
        body=body.strip()[:8000],
        metadata=metadata or {},
    )
    target = event_log_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(asdict(event), sort_keys=True) + "\n")
    return event


def event_to_dict(event: RoutedEvent) -> dict[str, Any]:
    return asdict(event)


def read_events(*, limit: int = 50, path: Path | None = None) -> list[dict[str, Any]]:
    target = event_log_path(path)
    if not target.exists():
        return []
    lines = target.read_text(encoding="utf-8").splitlines()
    events: list[dict[str, Any]] = []
    for line in reversed(lines[-max(limit * 2, limit) :]):
        try:
            raw = json.loads(line)
        except ValueError:
            continue
        if isinstance(raw, dict):
            events.append(raw)
        if len(events) >= limit:
            break
    return events
