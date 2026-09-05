from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from ash.config.models import VapiConfig
from ash.tools.base import ToolContext
from ash.tools.builtin.vapi import (
    VapiEndCallTool,
    VapiOutboundCallTool,
    _render_call_summary,
)


@pytest.mark.asyncio
async def test_vapi_outbound_requires_configuration() -> None:
    config = VapiConfig(enabled=True)

    result = await VapiOutboundCallTool(config).execute(
        {
            "customer_number": "+14155550100",
            "objective": "Ask about hours",
            "approved": True,
        },
        ToolContext(provider="telegram"),
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
async def test_vapi_outbound_dry_run_requires_no_credentials() -> None:
    config = VapiConfig(enabled=True, dry_run=True)

    result = await VapiOutboundCallTool(config).execute(
        {
            "customer_number": "+14155550100",
            "objective": "Ask whether walk-ins are accepted",
            "business_name": "Example Cafe",
            "approved": True,
        },
        ToolContext(provider="telegram"),
    )

    assert not result.is_error
    assert '"status": "dry_run"' in result.content
    assert "+14155550100" in result.content
    assert "Ask whether walk-ins are accepted" in result.content


@pytest.mark.asyncio
async def test_vapi_outbound_creates_call(monkeypatch) -> None:
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

    result = await VapiOutboundCallTool(config).execute(
        {
            "customer_number": "+14155550100",
            "objective": "Ask whether walk-ins are accepted",
            "business_name": "Example Cafe",
            "approved": True,
        },
        ToolContext(provider="telegram"),
    )

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
    assert captured["preflight"][0] == "https://api.vapi.ai/call"
    assert "key" not in result.content


@pytest.mark.asyncio
async def test_vapi_outbound_cleans_voice_text_and_passes_voicemail(
    monkeypatch,
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

    result = await VapiOutboundCallTool(config).execute(
        {
            "customer_number": "+14155550100",
            "objective": "Ask when he\x19s arriving\u2014then confirm.",
            "context": "Keep it\nbrief.",
            "customer_name": "Roshan",
            "voicemail_message": "Hi\u2014Rahul called. Please call back.",
            "approved": True,
        },
        ToolContext(provider="telegram"),
    )

    assert not result.is_error
    overrides = captured["payload"]["assistantOverrides"]
    assert overrides["variableValues"]["ash_objective"] == (
        "Ask when he's arriving-then confirm."
    )
    assert overrides["variableValues"]["ash_context"] == "Keep it brief."
    assert overrides["voicemailMessage"] == "Hi-Rahul called. Please call back."


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
async def test_vapi_outbound_blocks_duplicate_active_call(monkeypatch) -> None:
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

    result = await VapiOutboundCallTool(config).execute(
        {
            "customer_number": "+14155550100",
            "objective": "Ask about hours",
            "approved": True,
        },
        ToolContext(provider="telegram"),
    )

    assert result.is_error
    assert "already active" in result.content
    assert "call-active" in result.content
    post.assert_not_awaited()


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
    tool._send_telegram_summary = AsyncMock()

    await tool._watch_call(
        call_id="call-123",
        chat_id="12345",
        customer_number="+14155550100",
        business_name="Example Hardware",
        objective="Ask about hours",
    )

    assert calls == []
    tool._send_telegram_summary.assert_awaited_once()
    assert (
        "Summary: The store is open."
        in (tool._send_telegram_summary.await_args.args[1])
    )


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
async def test_vapi_end_call_stops_latest_matching_active_call(monkeypatch) -> None:
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
                [
                    {
                        "id": "other",
                        "status": "in-progress",
                        "assistantId": "other-assistant",
                        "phoneNumberId": "phone",
                        "createdAt": "2026-01-02T00:00:00Z",
                        "monitor": {
                            "controlUrl": "https://calls.vapi.ai/other/control"
                        },
                    },
                    {
                        "id": "call-123",
                        "status": "in-progress",
                        "assistantId": "assistant",
                        "phoneNumberId": "phone",
                        "createdAt": "2026-01-01T00:00:00Z",
                        "monitor": {
                            "controlUrl": "https://calls.vapi.ai/call-123/control"
                        },
                    },
                ]
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

    result = await VapiEndCallTool(config).execute({}, ToolContext(provider="telegram"))

    assert not result.is_error
    assert '"call_id": "call-123"' in result.content
    assert captured["post"] == (
        "https://calls.vapi.ai/call-123/control",
        {"type": "end-call"},
    )


@pytest.mark.asyncio
async def test_vapi_end_call_reports_when_no_call_is_active(monkeypatch) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return [{"id": "ended", "status": "ended"}]

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

    result = await VapiEndCallTool(config).execute({}, ToolContext(provider="telegram"))

    assert result.is_error
    assert "no active outbound call" in result.content.lower()


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
    assert "if search fails, use browser" in prompt
    assert "phone number in E.164 format" in prompt
    assert "ask whether they still want a phone confirmation" in prompt
