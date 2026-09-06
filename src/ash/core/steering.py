"""Per-conversation turn queue and cooperative cancellation state."""

from __future__ import annotations

import asyncio
import re
from collections import deque
from typing import Any

_CANCEL_RE = re.compile(
    r"^\s*(?:cancel|stop|abort|never\s*mind|nevermind|hang\s*up|end\s+the\s+call)\b",
    re.IGNORECASE,
)


def steering_text(item: Any) -> str:
    value = getattr(item, "text", item)
    return value if isinstance(value, str) else ""


def is_cancel_steering(item: Any) -> bool:
    return bool(_CANCEL_RE.search(steering_text(item)))


class TurnController[T]:
    """Own queued input exactly once and signal revisions to active work."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.cancel_event = asyncio.Event()
        self._revision_event = asyncio.Event()
        self._pending: deque[T] = deque()
        self._consumed_as_steering: deque[T] = deque()

    @property
    def pending_messages(self) -> list[T]:
        return list(self._pending)

    def begin_turn(self) -> None:
        self.cancel_event.clear()
        if any(is_cancel_steering(item) for item in self._pending):
            self.cancel_event.set()
        if not self._pending:
            self._revision_event.clear()

    def enqueue(self, item: T) -> None:
        self._pending.append(item)
        if is_cancel_steering(item):
            self.cancel_event.set()
        self._revision_event.set()

    def take_for_steering(self) -> list[T]:
        items = list(self._pending)
        self._pending.clear()
        self._consumed_as_steering.extend(items)
        self._revision_event.clear()
        return items

    def take_for_next_turn(self) -> list[T]:
        items = list(self._pending)
        self._pending.clear()
        self._revision_event.clear()
        return items

    def take_consumed_steering(self) -> list[T]:
        items = list(self._consumed_as_steering)
        self._consumed_as_steering.clear()
        return items

    async def wait_for_revision(self) -> None:
        await self._revision_event.wait()
