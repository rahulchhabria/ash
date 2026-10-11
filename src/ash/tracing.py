"""Vendor-neutral operation hooks. Spec: specs/integrations.md (Tracing)."""

from __future__ import annotations

import inspect
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, is_dataclass
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
_active: ContextVar[tuple[Any, ...]] = ContextVar("ash_trace_observers", default=())


def _safe(callback: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return callback(*args, **kwargs)
    except Exception as exc:
        logger.warning("trace_hook_failed", extra={"error.type": type(exc).__name__})
        return None


def serializable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, list):
        return [serializable(v) for v in value]
    return value


def record_usage(
    input_tokens: int, output_tokens: int, model: str | None = None
) -> None:
    for observer in _active.get():
        _safe(observer.usage, input_tokens, output_tokens, model)


@contextmanager
def operation(kind: str, name: str, data: dict[str, Any], observers: Any = None):
    selected = tuple(observers) if observers is not None else _active.get()
    children = tuple(
        child
        for observer in selected
        if (child := _safe(observer.start, kind, name, data)) is not None
    )
    token = _active.set(children)
    error = None
    try:
        yield children
    except BaseException as exc:
        error = type(exc).__name__
        raise
    finally:
        for child in reversed(children):
            _safe(child.finish, error)
        _active.reset(token)


def traced(kind: str):
    """Keep context across awaited calls and the lifetime of streamed iteration."""

    def decorate(fn: Any) -> Any:
        signature = inspect.signature(fn)

        def begin(args: Any, kwargs: Any):
            bound = signature.bind(*args, **kwargs).arguments
            owner = bound["self"]
            if kind != "agent" and not _active.get():
                return operation(kind, "", {})
            observers = None
            if kind == "agent":
                session = bound["session"]
                observers = getattr(owner, "_trace_observers", ())
                data = {
                    "conversation_id": session.session_id,
                    "provider": session.provider,
                    "input": bound["user_message"],
                }
                name = "ash.turn"
            elif kind == "llm":
                data = {
                    "model": bound.get("model") or owner.default_model,
                    "provider": owner.name,
                    "input": {
                        "messages": serializable(bound["messages"]),
                        "system": bound.get("system"),
                    },
                }
                name = f"{owner.name}.{fn.__name__}"
            else:
                name = bound["tool_name"]
                data = {"input": bound["input_data"]}
            return operation(kind, name, data, observers)

        if inspect.isasyncgenfunction(fn):

            @wraps(fn)
            async def streaming(*args: Any, **kwargs: Any):
                with begin(args, kwargs) as spans:
                    source = fn(*args, **kwargs)
                    try:
                        async for chunk in source:
                            for span in spans:
                                _safe(span.chunk, serializable(chunk))
                            yield chunk
                    finally:
                        await source.aclose()

            return streaming

        @wraps(fn)
        async def complete(*args: Any, **kwargs: Any):
            with begin(args, kwargs) as spans:
                result = await fn(*args, **kwargs)
                for span in spans:
                    _safe(span.result, serializable(result))
                return result

        return complete

    return decorate
