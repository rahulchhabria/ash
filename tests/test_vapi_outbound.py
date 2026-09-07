from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr

from ash.config.models import VapiConfig
from ash.sessions import SessionManager
from ash.sessions.types import OperationState, PendingCheckpointRecord
from ash.tools.base import ToolContext
from ash.tools.builtin.vapi import (
    VapiCallStatusTool,
    VapiEndCallTool,
    VapiOutboundCallTool,
    _call_idempotency_key,
    _canonical_request_from_mapping,
    _render_call_summary,
)


def _approved_call_context(
    tmp_path,
    input_data: dict,
    *,
    cancellation_event: asyncio.Event | None = None,
) -> tuple[ToolContext, SessionManager, dict]:
    manager = SessionManager(
        provider="telegram",
        chat_id="chat",
        user_id="user",
        sessions_path=tmp_path,
    )
    approval_request = _canonical_request_from_mapping(input_data)
    checkpoint_id = "checkpoint-approved-call"
    checkpoint = {
        "checkpoint_id": checkpoint_id,
        "prompt": "Place this call?",
        "options": ["Call now", "Don't call"],
        "approval_request": approval_request,
    }
    manager.save_pending_checkpoint(
        PendingCheckpointRecord(
            checkpoint_id=checkpoint_id,
            prompt="Place this call?",
            options=checkpoint["options"],
            checkpoint=checkpoint,
            status="claimed",
            selected_option="Call now",
        )
    )
    return (
        ToolContext(
            provider="telegram",
            session_id=manager.session_key,
            session_manager=manager,
            cancellation_event=cancellation_event,
            metadata={
                "approval_grant": {
                    "checkpoint_id": checkpoint_id,
                    "approval_request": approval_request,
                }
            },
        ),
        manager,
        approval_request,
    )


@pytest.mark.asyncio
async def test_vapi_outbound_requires_configuration(tmp_path) -> None:
    config = VapiConfig(enabled=True)
    input_data = {
        "customer_number": "+14155550100",
        "objective": "Ask about hours",
        "allow_ivr_navigation": False,
    }
    context, _, _ = _approved_call_context(tmp_path, input_data)

    result = await VapiOutboundCallTool(config).execute(
        input_data,
        context,
    )

    assert result.is_error
    assert "VAPI_API_KEY" in result.content


@pytest.mark.asyncio
async def test_vapi_outbound_validates_e164() -> None:
    config = VapiConfig(
        enabled=True,
        api_key=SecretStr("key"),
        assistant_id="assistant",
        phone_number_id="phone",
    )

    result = await VapiOutboundCallTool(config).execute(
        {
            "customer_number": "415-555-0100",
            "objective": "Ask about hours",
            "approved": True,
        },
        ToolContext(provider="telegram"),
    )

    assert result.is_error
    assert "E.164" in result.content


@pytest.mark.asyncio
async def test_vapi_outbound_rejects_model_supplied_approval() -> None:
    result = await VapiOutboundCallTool(VapiConfig(enabled=True, dry_run=True)).execute(
        {
            "customer_number": "+14155550100",
            "objective": "Ask about hours",
            "allow_ivr_navigation": False,
            "approved": True,
        },
        ToolContext(provider="telegram"),
    )

    assert result.is_error
    assert "checkpoint approval" in result.content


@pytest.mark.asyncio
async def test_vapi_outbound_rejects_changed_approved_request(tmp_path) -> None:
    approved_input = {
        "customer_number": "+14155550100",
        "objective": "Ask about hours",
        "allow_ivr_navigation": False,
    }
    context, manager, _ = _approved_call_context(tmp_path, approved_input)

    result = await VapiOutboundCallTool(VapiConfig(enabled=True, dry_run=True)).execute(
        {**approved_input, "objective": "Buy an item"},
        context,
    )

    assert result.is_error
    assert "matching" in result.content
    record = manager.get_pending_checkpoint("checkpoint-approved-call")
    assert record is not None
    assert record.approval_consumed_at is None


@pytest.mark.asyncio
async def test_vapi_outbound_dry_run_requires_no_credentials(tmp_path) -> None:
    config = VapiConfig(enabled=True, dry_run=True)
    input_data = {
        "customer_number": "+14155550100",
        "objective": "Ask whether walk-ins are accepted",
        "business_name": "Example Cafe",
        "allow_ivr_navigation": False,
    }
    context, manager, _ = _approved_call_context(tmp_path, input_data)

    result = await VapiOutboundCallTool(config).execute(
        input_data,
        context,
    )

    assert not result.is_error
    assert '"status": "dry_run"' in result.content
    assert "+14155550100" in result.content
    assert "Ask whether walk-ins are accepted" in result.content
    record = manager.get_pending_checkpoint("checkpoint-approved-call")
    assert record is not None
    assert record.approval_consumed_at is not None

    replay = await VapiOutboundCallTool(config).execute(input_data, context)
    assert replay.is_error
    assert "approval" in replay.content.lower()


@pytest.mark.asyncio
async def test_vapi_outbound_creates_call(monkeypatch, tmp_path) -> None:
    captured = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {"id": "call-123", "status": "queued"}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            captured["operation_before_client_exit"] = manager.get_operation("call-123")
            return None

        async def get(self, url, *, headers, params):
            captured["preflight"] = (url, headers, params)
            return FakeResponse()

        async def post(self, url, *, headers, json):
            captured.update(url=url, headers=headers, payload=json)
            return FakeResponse()

    monkeypatch.setattr(
        "ash.tools.builtin.vapi.httpx.AsyncClient",
        lambda **kwargs: FakeClient(),
    )
    config = VapiConfig(
        enabled=True,
        api_key=SecretStr("key"),
        assistant_id="assistant",
        phone_number_id="phone",
    )

    input_data = {
        "customer_number": "+14155550100",
        "objective": "Ask whether walk-ins are accepted",
        "business_name": "Example Cafe",
        "allow_ivr_navigation": True,
    }
    context, manager, _ = _approved_call_context(tmp_path, input_data)
    tool = VapiOutboundCallTool(config)
    result = await tool.execute(input_data, context)

    assert not result.is_error
    assert "call-123" in result.content
    assert captured["url"] == "https://api.vapi.ai/call"
    assert (
        captured["payload"]["assistantOverrides"]["variableValues"]["ash_objective"]
        == "Ask whether walk-ins are accepted"
    )
    assert (
        captured["payload"]["assistantOverrides"]["variableValues"]["objective"]
        == "Ask whether walk-ins are accepted"
    )
    assert captured["payload"]["assistantOverrides"]["firstMessageMode"] == (
        "assistant-speaks-first-with-model-generated-message"
    )
    assert (
        captured["payload"]["assistantOverrides"]["variableValues"][
            "ash_ivr_navigation"
        ]
        == "routing-only"
    )
    assert captured["preflight"][0] == "https://api.vapi.ai/call"
    assert captured["operation_before_client_exit"] is not None
    assert "key" not in result.content
    operation = manager.get_operation("call-123")
    assert operation is not None
    assert operation.metadata == {
        "business_name": "Example Cafe",
        "summary_chat_id": "chat",
        "summary_delivery": "disabled",
    }
    assert tool._summary_tasks == {}
    assert tool.recover_pending_summaries(tmp_path) == 0


@pytest.mark.asyncio
async def test_vapi_outbound_honors_cancellation_before_post(
    monkeypatch, tmp_path
) -> None:
    cancellation_event = asyncio.Event()

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, *, headers, params):
            cancellation_event.set()
            return FakeResponse()

        async def post(self, url, *, headers, json):
            pytest.fail("A cancelled call must not be placed")

    monkeypatch.setattr(
        "ash.tools.builtin.vapi.httpx.AsyncClient",
        lambda **kwargs: FakeClient(),
    )
    config = VapiConfig(
        enabled=True,
        api_key=SecretStr("key"),
        assistant_id="assistant",
        phone_number_id="phone",
    )

    input_data = {
        "customer_number": "+14155550100",
        "objective": "Ask whether walk-ins are accepted",
        "allow_ivr_navigation": False,
    }
    context, _, _ = _approved_call_context(
        tmp_path, input_data, cancellation_event=cancellation_event
    )
    result = await VapiOutboundCallTool(config).execute(input_data, context)

    assert result.is_error
    assert "cancelled before placement" in result.content


@pytest.mark.asyncio
async def test_vapi_outbound_cleans_voice_text_and_passes_voicemail(
    monkeypatch, tmp_path
) -> None:
    captured = {}

    class FakeResponse:
        def __init__(self, payload=None):
            self._payload = [] if payload is None else payload

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return self._payload

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, *, headers, params):
            return FakeResponse()

        async def post(self, url, *, headers, json):
            captured["payload"] = json
            return FakeResponse({"id": "call-123", "status": "queued"})

    monkeypatch.setattr(
        "ash.tools.builtin.vapi.httpx.AsyncClient",
        lambda **kwargs: FakeClient(),
    )
    config = VapiConfig(
        enabled=True,
        api_key=SecretStr("key"),
        assistant_id="assistant",
        phone_number_id="phone",
    )

    input_data = {
        "customer_number": "+14155550100",
        "objective": "Ask when he\x19s arriving\u2014then confirm.",
        "context": "Keep it\nbrief.",
        "customer_name": "Roshan",
        "voicemail_message": "Hi\u2014Rahul called. Please call back.",
        "allow_ivr_navigation": False,
    }
    context, _, _ = _approved_call_context(tmp_path, input_data)
    result = await VapiOutboundCallTool(config).execute(input_data, context)

    assert not result.is_error
    overrides = captured["payload"]["assistantOverrides"]
    assert overrides["variableValues"]["ash_objective"] == (
        "Ask when he's arriving-then confirm."
    )
    assert overrides["variableValues"]["ash_context"] == "Keep it brief."
    assert overrides["variableValues"]["ash_ivr_navigation"] == "disabled"
    assert overrides["voicemailMessage"] == "Hi-Rahul called. Please call back."


def test_vapi_outbound_requires_explicit_ivr_policy() -> None:
    schema = VapiOutboundCallTool(VapiConfig()).input_schema

    assert "allow_ivr_navigation" in schema["required"]
    description = schema["properties"]["allow_ivr_navigation"]["description"]
    assert "non-consequential IVR routing" in description


@pytest.mark.asyncio
async def test_vapi_outbound_rejects_unresolved_placeholders() -> None:
    config = VapiConfig(enabled=True, dry_run=True)

    result = await VapiOutboundCallTool(config).execute(
        {
            "customer_number": "+14155550100",
            "objective": "Ask Roshan what time he is arriving",
            "context": "Say this is <name>, <relationship>.",
            "approved": True,
        },
        ToolContext(provider="telegram"),
    )

    assert result.is_error
    assert "unresolved placeholder" in result.content


@pytest.mark.asyncio
async def test_vapi_outbound_blocks_duplicate_active_call(
    monkeypatch, tmp_path
) -> None:
    post = AsyncMock()

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return [
                {
                    "id": "call-active",
                    "status": "in-progress",
                    "assistantId": "assistant",
                    "phoneNumberId": "phone",
                    "customer": {"number": "+14155550100"},
                    "createdAt": "2026-09-05T17:44:50Z",
                }
            ]

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, *, headers, params):
            return FakeResponse()

        async def post(self, url, *, headers, json):
            return await post(url, headers=headers, json=json)

    monkeypatch.setattr(
        "ash.tools.builtin.vapi.httpx.AsyncClient",
        lambda **kwargs: FakeClient(),
    )
    config = VapiConfig(
        enabled=True,
        api_key=SecretStr("key"),
        assistant_id="assistant",
        phone_number_id="phone",
    )

    input_data = {
        "customer_number": "+14155550100",
        "objective": "Ask about hours",
        "allow_ivr_navigation": False,
    }
    context, manager, _ = _approved_call_context(tmp_path, input_data)
    result = await VapiOutboundCallTool(config).execute(input_data, context)

    assert result.is_error
    assert "already active" in result.content
    assert "call-active" not in result.content
    assert manager.get_operation("call-active") is None
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_vapi_outbound_reuses_only_owned_active_call(
    monkeypatch, tmp_path
) -> None:
    input_data = {
        "customer_number": "+14155550100",
        "objective": "Ask about hours",
        "allow_ivr_navigation": False,
    }
    context, manager, approval_request = _approved_call_context(tmp_path, input_data)
    operation_key = _call_idempotency_key(manager.session_key, approval_request)
    post = AsyncMock()

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return [
                {
                    "id": "call-owned",
                    "status": "in-progress",
                    "assistantId": "assistant",
                    "phoneNumberId": "phone",
                    "customer": {"number": "+14155550100"},
                    "createdAt": "2026-09-05T17:44:50Z",
                    "assistantOverrides": {
                        "variableValues": {
                            "ash_conversation_id": manager.session_key,
                            "ash_operation_key": operation_key,
                        }
                    },
                }
            ]

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, *, headers, params):
            return FakeResponse()

        async def post(self, url, *, headers, json):
            return await post(url, headers=headers, json=json)

    monkeypatch.setattr(
        "ash.tools.builtin.vapi.httpx.AsyncClient",
        lambda **kwargs: FakeClient(),
    )
    config = VapiConfig(
        enabled=True,
        api_key=SecretStr("key"),
        assistant_id="assistant",
        phone_number_id="phone",
    )

    result = await VapiOutboundCallTool(config).execute(input_data, context)

    assert not result.is_error
    assert "call-owned" in result.content
    operation = manager.get_operation("call-owned")
    assert operation is not None
    assert operation.idempotency_key == operation_key
    record = manager.get_pending_checkpoint("checkpoint-approved-call")
    assert record is not None
    assert record.approval_consumed_at is not None
    post.assert_not_awaited()


def test_vapi_idempotency_includes_objective() -> None:
    first = _canonical_request_from_mapping(
        {
            "customer_number": "+14155550100",
            "objective": "Ask about hours",
            "allow_ivr_navigation": False,
        }
    )
    second = {**first, "objective": "Ask about inventory"}

    assert _call_idempotency_key("conversation", first) != _call_idempotency_key(
        "conversation", second
    )


@pytest.mark.asyncio
async def test_vapi_outbound_reuses_recent_durable_operation(
    monkeypatch, tmp_path
) -> None:
    input_data = {
        "customer_number": "+14155550100",
        "objective": "Ask about hours",
        "allow_ivr_navigation": False,
    }
    context, manager, approval_request = _approved_call_context(tmp_path, input_data)
    manager.record_operation(
        OperationState(
            kind="vapi_call",
            operation_id="call-existing",
            status="ended",
            idempotency_key=_call_idempotency_key(
                manager.session_key, approval_request
            ),
            destination="+14155550100",
            objective="Ask about hours",
        )
    )
    monkeypatch.setattr(
        "ash.tools.builtin.vapi.httpx.AsyncClient",
        lambda **kwargs: pytest.fail("Vapi must not be called for a duplicate"),
    )

    result = await VapiOutboundCallTool(VapiConfig(enabled=True, dry_run=True)).execute(
        input_data,
        context,
    )

    assert not result.is_error
    assert '"reused_existing_operation": true' in result.content
    assert "call-existing" in result.content


@pytest.mark.asyncio
async def test_vapi_outbound_requires_operation_id_for_explicit_retry(tmp_path) -> None:
    input_data = {
        "customer_number": "+14155550100",
        "objective": "Try the call again",
        "allow_ivr_navigation": False,
        "retry_operation_id": "call-ended",
    }
    context, manager, _ = _approved_call_context(tmp_path, input_data)
    manager.record_operation(
        OperationState(
            kind="vapi_call",
            operation_id="call-ended",
            status="ended",
            idempotency_key="key",
            destination="+14155550100",
        )
    )

    result = await VapiOutboundCallTool(VapiConfig(enabled=True, dry_run=True)).execute(
        input_data,
        context,
    )

    assert not result.is_error
    assert '"status": "dry_run"' in result.content


@pytest.mark.asyncio
async def test_vapi_status_uses_and_updates_durable_call_record(
    monkeypatch, tmp_path
) -> None:
    captured = {}

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {
                "id": "call-123",
                "status": "ended",
                "assistantId": "assistant",
                "phoneNumberId": "phone",
                "endedReason": "assistant-ended-call",
                "analysis": {"summary": "The store has size 10 in stock."},
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, *, headers):
            captured["url"] = url
            return FakeResponse()

    monkeypatch.setattr(
        "ash.tools.builtin.vapi.httpx.AsyncClient",
        lambda **kwargs: FakeClient(),
    )
    manager = SessionManager(
        provider="telegram",
        chat_id="chat",
        user_id="user",
        sessions_path=tmp_path,
    )
    manager.record_operation(
        OperationState(
            kind="vapi_call",
            operation_id="call-123",
            status="queued",
            idempotency_key="key",
            destination="+14155550100",
        )
    )
    config = VapiConfig(
        enabled=True,
        api_key=SecretStr("key"),
        assistant_id="assistant",
        phone_number_id="phone",
    )

    result = await VapiCallStatusTool(config).execute(
        {},
        ToolContext(
            provider="telegram",
            session_id=manager.session_key,
            session_manager=manager,
        ),
    )

    assert not result.is_error
    assert captured["url"].endswith("/call/call-123")
    assert "size 10 in stock" in result.content
    operation = manager.get_operation("call-123")
    assert operation is not None
    assert operation.status == "ended"


@pytest.mark.asyncio
async def test_vapi_status_rejects_call_outside_conversation(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(
        "ash.tools.builtin.vapi.httpx.AsyncClient",
        lambda **kwargs: pytest.fail("Foreign calls must not be queried"),
    )
    manager = SessionManager(
        provider="telegram",
        chat_id="chat",
        user_id="user",
        sessions_path=tmp_path,
    )
    config = VapiConfig(
        enabled=True,
        api_key=SecretStr("key"),
        assistant_id="assistant",
        phone_number_id="phone",
    )

    result = await VapiCallStatusTool(config).execute(
        {"call_id": "call-from-another-conversation"},
        ToolContext(
            provider="telegram",
            session_id=manager.session_key,
            session_manager=manager,
        ),
    )

    assert result.is_error
    assert "not recorded in this conversation" in result.content


def test_vapi_call_summary_includes_actions() -> None:
    text = _render_call_summary(
        {
            "status": "ended",
            "endedReason": "assistant-ended-call",
            "analysis": {
                "summary": "The shop has the item in stock until 5 PM.",
                "structuredData": {
                    "actionItems": ["Pick it up before 5 PM", "Ask for Sam"]
                },
            },
        },
        call_id="call-123",
        customer_number="+14155550100",
        business_name="Example Hardware",
        objective="Check stock",
    )

    assert "Call complete: Example Hardware" in text
    assert "The shop has the item in stock until 5 PM." in text
    assert "Action needed: Pick it up before 5 PM; Ask for Sam" in text
    assert "Call ID: call-123" in text


def test_vapi_call_summary_falls_back_to_transcript() -> None:
    text = _render_call_summary(
        {
            "status": "ended",
            "endedReason": "customer-did-not-answer",
            "artifact": {"transcript": "The number rang without an answer."},
        },
        call_id="call-456",
        customer_number="+14155550101",
        business_name="",
        objective="Ask about hours",
    )

    assert "Call complete: +14155550101" in text
    assert "Summary: The number rang without an answer." in text
    assert "Action needed: None identified." in text


@pytest.mark.asyncio
async def test_vapi_summary_watcher_waits_for_analysis(monkeypatch) -> None:
    calls = [
        {"status": "ended", "analysis": {}, "artifact": {"transcript": "raw"}},
        {
            "status": "ended",
            "endedReason": "assistant-ended-call",
            "analysis": {
                "summary": "The store is open.",
                "structuredData": {"actionItems": []},
            },
        },
    ]

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return self._payload

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, *, headers):
            return FakeResponse(calls.pop(0))

    monkeypatch.setattr(
        "ash.tools.builtin.vapi.httpx.AsyncClient",
        lambda **kwargs: FakeClient(),
    )
    monkeypatch.setattr("ash.tools.builtin.vapi.POLL_INTERVAL_SECONDS", 0)

    config = VapiConfig(enabled=True, api_key=SecretStr("key"))
    tool = VapiOutboundCallTool(config, telegram_bot_token="telegram-key")
    send_summary = AsyncMock()
    monkeypatch.setattr(tool, "_send_telegram_summary", send_summary)

    await tool._watch_call(
        call_id="call-123",
        chat_id="12345",
        customer_number="+14155550100",
        business_name="Example Hardware",
        objective="Ask about hours",
    )

    assert calls == []
    send_summary.assert_awaited_once()
    assert send_summary.await_args is not None
    assert "Summary: The store is open." in send_summary.await_args.args[1]


@pytest.mark.asyncio
async def test_vapi_summary_delivery_retries_transient_telegram_failure(
    monkeypatch, tmp_path
) -> None:
    manager = SessionManager(
        provider="telegram",
        chat_id="chat",
        user_id="user",
        sessions_path=tmp_path,
    )
    await manager.ensure_session()
    manager.record_operation(
        OperationState(
            kind="vapi_call",
            operation_id="call-retry-summary",
            status="ended",
            idempotency_key="summary-retry-key",
            destination="+14155550100",
            objective="Ask about seating",
            metadata={"summary_delivery": "pending"},
        )
    )
    tool = VapiOutboundCallTool(
        VapiConfig(enabled=True, api_key=SecretStr("key")),
        telegram_bot_token="telegram-key",
    )
    send_summary = AsyncMock(
        side_effect=[httpx.ConnectError("temporary Telegram outage"), None]
    )
    monkeypatch.setattr(tool, "_send_telegram_summary", send_summary)
    monkeypatch.setattr("ash.tools.builtin.vapi.TELEGRAM_RETRY_INITIAL_SECONDS", 0)

    await tool._deliver_telegram_summary(
        call_id="call-retry-summary",
        chat_id="chat",
        text="No wait for a party of 12.",
        session_manager=manager,
    )

    assert send_summary.await_count == 2
    operation = manager.get_operation("call-retry-summary")
    assert operation is not None
    assert operation.metadata["summary_delivery"] == "delivered"


@pytest.mark.asyncio
async def test_vapi_summary_watcher_recovers_after_restart(
    monkeypatch, tmp_path
) -> None:
    manager = SessionManager(
        provider="telegram",
        chat_id="chat",
        user_id="user",
        thread_id="thread",
        sessions_path=tmp_path,
    )
    await manager.ensure_session()
    manager.record_operation(
        OperationState(
            kind="vapi_call",
            operation_id="call-restart",
            status="in-progress",
            idempotency_key="stable-key",
            destination="+14155550100",
            objective="Ask about seating",
            metadata={
                "business_name": "Example Cafe",
                "summary_chat_id": "chat",
                "summary_delivery": "pending",
            },
        )
    )
    config = VapiConfig(enabled=True, api_key=SecretStr("key"))

    monkeypatch.setattr("ash.tools.builtin.vapi.POLL_INTERVAL_SECONDS", 3600)
    before_restart = VapiOutboundCallTool(config, telegram_bot_token="telegram-key")
    before_restart._start_summary_watcher(
        call_id="call-restart",
        chat_id="chat",
        customer_number="+14155550100",
        business_name="Example Cafe",
        objective="Ask about seating",
        session_manager=manager,
    )
    await asyncio.sleep(0)
    await before_restart.shutdown()
    pending = manager.get_operation("call-restart")
    assert pending is not None
    assert pending.metadata["summary_delivery"] == "pending"

    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {
                "id": "call-restart",
                "status": "ended",
                "endedReason": "assistant-ended-call",
                "analysis": {
                    "summary": "A party of 12 can be seated with no wait.",
                    "structuredData": {"actionItems": []},
                },
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, *, headers):
            return FakeResponse()

    monkeypatch.setattr(
        "ash.tools.builtin.vapi.httpx.AsyncClient",
        lambda **kwargs: FakeClient(),
    )
    monkeypatch.setattr("ash.tools.builtin.vapi.POLL_INTERVAL_SECONDS", 0)

    after_restart = VapiOutboundCallTool(config, telegram_bot_token="telegram-key")
    send_summary = AsyncMock()
    monkeypatch.setattr(after_restart, "_send_telegram_summary", send_summary)
    recovered = after_restart.recover_pending_summaries(tmp_path)
    tasks = list(after_restart._summary_tasks.values())
    await asyncio.gather(*tasks)

    assert recovered == 1
    send_summary.assert_awaited_once()
    assert send_summary.await_args is not None
    assert "party of 12 can be seated with no wait" in send_summary.await_args.args[1]
    restored = manager.get_operation("call-restart")
    assert restored is not None
    assert restored.status == "ended"
    assert restored.metadata["summary_delivery"] == "delivered"
    assert "summary_delivered_at" in restored.metadata
    assert after_restart.recover_pending_summaries(tmp_path) == 0


@pytest.mark.asyncio
async def test_vapi_outbound_requires_explicit_telegram_approval() -> None:
    config = VapiConfig(
        enabled=True,
        api_key=SecretStr("key"),
        assistant_id="assistant",
        phone_number_id="phone",
    )

    result = await VapiOutboundCallTool(config).execute(
        {"customer_number": "+14155550100", "objective": "Ask about hours"},
        ToolContext(provider="telegram"),
    )

    assert result.is_error
    assert "approval" in result.content.lower()


@pytest.mark.asyncio
async def test_vapi_end_call_stops_latest_matching_active_call(
    monkeypatch, tmp_path
) -> None:
    captured = {}

    class FakeResponse:
        text = ""

        def __init__(self, payload=None):
            self._payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return self._payload

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, *, headers, params=None):
            captured["get"] = (url, headers, params)
            return FakeResponse(
                {
                    "id": "call-123",
                    "status": "in-progress",
                    "assistantId": "assistant",
                    "phoneNumberId": "phone",
                    "createdAt": "2026-01-01T00:00:00Z",
                    "monitor": {"controlUrl": "https://calls.vapi.ai/call-123/control"},
                }
            )

        async def post(self, url, *, json):
            captured["post"] = (url, json)
            return FakeResponse()

    monkeypatch.setattr(
        "ash.tools.builtin.vapi.httpx.AsyncClient",
        lambda **kwargs: FakeClient(),
    )
    config = VapiConfig(
        enabled=True,
        api_key=SecretStr("key"),
        assistant_id="assistant",
        phone_number_id="phone",
    )
    manager = SessionManager(
        provider="telegram",
        chat_id="chat",
        user_id="user",
        sessions_path=tmp_path,
    )
    manager.record_operation(
        OperationState(
            kind="vapi_call",
            operation_id="call-123",
            status="in-progress",
            idempotency_key="key",
        )
    )

    result = await VapiEndCallTool(config).execute(
        {}, ToolContext(provider="telegram", session_manager=manager)
    )

    assert not result.is_error
    assert '"call_id": "call-123"' in result.content
    assert captured["post"] == (
        "https://calls.vapi.ai/call-123/control",
        {"type": "end-call"},
    )


@pytest.mark.asyncio
async def test_vapi_end_call_reports_when_no_call_is_active(
    monkeypatch, tmp_path
) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {
                "id": "ended",
                "status": "ended",
                "assistantId": "assistant",
                "phoneNumberId": "phone",
            }

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, *, headers, params=None):
            return FakeResponse()

    monkeypatch.setattr(
        "ash.tools.builtin.vapi.httpx.AsyncClient",
        lambda **kwargs: FakeClient(),
    )
    config = VapiConfig(
        enabled=True,
        api_key=SecretStr("key"),
        assistant_id="assistant",
        phone_number_id="phone",
    )
    manager = SessionManager(
        provider="telegram",
        chat_id="chat",
        user_id="user",
        sessions_path=tmp_path,
    )
    manager.record_operation(
        OperationState(
            kind="vapi_call",
            operation_id="ended",
            status="in-progress",
            idempotency_key="key",
        )
    )

    result = await VapiEndCallTool(config).execute(
        {}, ToolContext(provider="telegram", session_manager=manager)
    )

    assert result.is_error
    assert "no active outbound call" in result.content.lower()


@pytest.mark.asyncio
async def test_vapi_end_call_rejects_call_outside_conversation(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(
        "ash.tools.builtin.vapi.httpx.AsyncClient",
        lambda **kwargs: pytest.fail("Foreign calls must not be queried"),
    )
    manager = SessionManager(
        provider="telegram",
        chat_id="chat",
        user_id="user",
        sessions_path=tmp_path,
    )
    config = VapiConfig(
        enabled=True,
        api_key=SecretStr("key"),
        assistant_id="assistant",
        phone_number_id="phone",
    )

    result = await VapiEndCallTool(config).execute(
        {"call_id": "call-from-another-conversation"},
        ToolContext(provider="telegram", session_manager=manager),
    )

    assert result.is_error
    assert "not recorded in this conversation" in result.content


def test_vapi_end_call_rejects_untrusted_control_url() -> None:
    from ash.tools.builtin.vapi import _control_url

    assert (
        _control_url(
            {"monitor": {"controlUrl": "https://example.com/steal-credentials"}}
        )
        is None
    )


def test_conduit_agent_requires_approval_tools() -> None:
    from ash.agents.builtin.conduit import ConduitAgent

    config = ConduitAgent().config

    assert config.supports_checkpointing is True
    assert "interrupt" in config.allowed_tools
    assert "browser" in config.allowed_tools
    assert "vapi_outbound_call" in config.allowed_tools
    assert "vapi_end_call" in config.allowed_tools


def test_conduit_agent_instructs_place_resolution_before_calls() -> None:
    from ash.agents.builtin.conduit import ConduitAgent

    prompt = ConduitAgent().config.system_prompt

    assert "If the user names a business/place without a phone number" in prompt
    assert "Parallel first, then hosted OpenAI search" in prompt
    assert "Use browser only if both search backends" in prompt
    assert "phone number in E.164 format" in prompt
    assert "ordinary routing-only IVR navigation" in prompt
    assert "Set allow_ivr_navigation=true only" in prompt
    assert "ask whether they still want a phone confirmation" in prompt
