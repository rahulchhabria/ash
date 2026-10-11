from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from ash.config.models import BraintrustConfig
from ash.observability.tracing import BraintrustObserver, SentryObserver
from ash.tracing import _active, operation, record_usage, traced


class Provider:
    name = "fake"
    default_model = "test-model"

    @traced("llm")
    async def complete(self, messages, *, model=None):
        await asyncio.sleep(0)
        return {
            "message": {"role": "assistant", "content": "hello"},
            "usage": {"input_tokens": 3, "output_tokens": 2},
            "model": "actual-model",
        }

    @traced("llm")
    async def stream(self, messages):
        yield {"type": "text_delta", "content": "token="}
        await asyncio.sleep(0)
        yield {"type": "text_delta", "content": "private-secret"}
        record_usage(5, 4, "actual-model")


class Agent:
    def __init__(self, observer):
        self._trace_observers = [observer]

    @traced("agent")
    async def process(self, user_message, session):
        await Provider().complete([user_message])
        return {"text": session.session_id}

    @traced("agent")
    async def stream(self, user_message, session):
        async for chunk in Provider().stream([user_message]):
            yield chunk


def observer():
    root = Mock()
    root.start_span.side_effect = lambda **kw: Mock()
    return root, BraintrustObserver(root, BraintrustConfig(capture_content=True), {})


@pytest.mark.asyncio
async def test_nested_spans_and_concurrent_turns_are_isolated():
    root, sink = observer()
    agent = Agent(sink)
    results = await asyncio.gather(
        *[
            agent.process("hello", SimpleNamespace(session_id=str(i), provider="test"))
            for i in range(2)
        ]
    )
    assert results == [{"text": "0"}, {"text": "1"}]
    assert _active.get() == ()
    assert root.start_span.call_count == 2
    assert {
        c.kwargs["metadata"]["conversation_id"] for c in root.start_span.call_args_list
    } == {"0", "1"}


@pytest.mark.asyncio
async def test_stream_content_is_scrubbed_after_reassembly_and_usage_preserved():
    parent = Mock()
    sink = BraintrustObserver(parent, BraintrustConfig(capture_content=True), {})
    with operation("agent", "turn", {"conversation_id": "1"}, [sink]):
        chunks = [c async for c in Provider().stream(["hi"])]
    assert len(chunks) == 2
    child = parent.start_span.return_value.start_span.return_value
    calls = str(child.log.call_args_list)
    assert "private-secret" not in calls
    assert "prompt_tokens" in calls and "actual-model" in calls
    assert child.end.call_count == 1


@pytest.mark.asyncio
async def test_cancelled_stream_closes_root_and_llm():
    parent, sink = observer()
    source = Agent(sink).stream(
        "hi", SimpleNamespace(session_id="cancel", provider="test")
    )
    await anext(source)
    await source.aclose()
    assert _active.get() == ()


def test_content_opt_out_and_tool_error():
    parent = Mock()
    sink = BraintrustObserver(parent, BraintrustConfig(), {})
    with operation(
        "tool", "broken", {"input": {"password": "private"}}, [sink]
    ) as spans:
        spans[0].result({"is_error": True, "content": "private"})
    assert "private" not in str(parent.mock_calls)
    parent.start_span.return_value.log.assert_called_once_with(
        error="Tool returned an error"
    )


@pytest.mark.asyncio
async def test_observer_failures_do_not_change_result():
    broken = Mock()
    broken.start.side_effect = RuntimeError("offline")
    result = await Agent(broken).process(
        "hi", SimpleNamespace(session_id="ok", provider="test")
    )
    assert result == {"text": "ok"}
    assert _active.get() == ()


def test_sentry_root_tool_and_error_status():
    import sentry_sdk

    events = []
    with sentry_sdk.init(
        dsn="http://key@localhost/1",
        traces_sample_rate=1,
        default_integrations=False,
        before_send_transaction=lambda event, hint: events.append(event) or None,
    ):
        with operation(
            "agent", "ash.turn", {"conversation_id": "test"}, [SentryObserver()]
        ):
            with operation("tool", "test_tool", {}) as spans:
                spans[0].result({"is_error": True})
        sentry_sdk.flush()
    assert events[0]["contexts"]["trace"]["op"] == "gen_ai.invoke_agent"
    assert events[0]["spans"][0]["op"] == "gen_ai.execute_tool"
    assert events[0]["spans"][0]["status"] == "internal_error"
