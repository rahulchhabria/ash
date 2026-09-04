from __future__ import annotations

import pytest
from pydantic import SecretStr

from ash.config.models import VapiConfig
from ash.tools.base import ToolContext
from ash.tools.builtin.vapi import VapiOutboundCallTool


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
    assert "key" not in result.content


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


def test_conduit_agent_requires_approval_tools() -> None:
    from ash.agents.builtin.conduit import ConduitAgent

    config = ConduitAgent().config

    assert config.supports_checkpointing is True
    assert "interrupt" in config.allowed_tools
    assert "browser" in config.allowed_tools
    assert "vapi_outbound_call" in config.allowed_tools


def test_conduit_agent_instructs_place_resolution_before_calls() -> None:
    from ash.agents.builtin.conduit import ConduitAgent

    prompt = ConduitAgent().config.system_prompt

    assert "If the user names a business/place without a phone number" in prompt
    assert "if search fails, use browser" in prompt
    assert "phone number in E.164 format" in prompt
    assert "ask whether they still want a phone confirmation" in prompt
