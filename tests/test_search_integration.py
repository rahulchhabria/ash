from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from ash.config import AshConfig
from ash.config.models import (
    ExaSearchConfig,
    GooglePlacesConfig,
    ModelConfig,
    ParallelSearchConfig,
    SandboxConfig,
)
from ash.core.prompt import PromptContext
from ash.core.session import SessionState
from ash.integrations import IntegrationContext, SearchIntegration
from ash.sandbox import ExecutionResult
from ash.tools import ToolRegistry
from ash.tools.base import ToolContext
from ash.tools.builtin.exa_search import EXA_SEARCH_SCRIPT, ExaSearchTool
from ash.tools.builtin.google_places import GOOGLE_PLACES_SCRIPT, GooglePlacesTool
from ash.tools.builtin.search_cache import SearchCache


def _context(config: AshConfig) -> IntegrationContext:
    components = cast(Any, SimpleNamespace(tool_registry=ToolRegistry()))
    return IntegrationContext(config=config, components=components, mode="chat")


def test_provider_scripts_reject_redirects() -> None:
    compile(EXA_SEARCH_SCRIPT, "<exa-search>", "exec")
    compile(GOOGLE_PLACES_SCRIPT, "<google-places>", "exec")
    assert "_NoRedirectHandler" in EXA_SEARCH_SCRIPT
    assert "_NoRedirectHandler" in GOOGLE_PLACES_SCRIPT


@pytest.mark.asyncio
async def test_search_integration_registers_default_hosted_search_and_network_fetch(
    tmp_path,
) -> None:
    config = AshConfig(
        workspace=tmp_path,
        models={"default": ModelConfig(provider="openai", model="gpt-5-mini")},
    )
    context = _context(config)

    await SearchIntegration().setup(context)

    registry = context.components.tool_registry
    assert registry.has("openai_web_search")
    assert registry.has("web_fetch")
    assert (
        cast(Any, registry.get("web_fetch"))._executor._config.network_mode == "bridge"
    )
    assert not registry.has("web_search")
    assert not registry.has("exa_search")
    assert not registry.has("google_places")


@pytest.mark.asyncio
async def test_non_openai_provider_does_not_register_or_advertise_hosted_search(
    tmp_path,
) -> None:
    config = AshConfig(
        workspace=tmp_path,
        models={"default": ModelConfig(provider="anthropic", model="claude-test")},
    )
    context = _context(config)
    integration = SearchIntegration()
    await integration.setup(context)

    assert not context.components.tool_registry.has("openai_web_search")
    prompt = integration.augment_prompt_context(
        PromptContext(), SessionState("s", "cli", "c", "u"), context
    )
    assert "openai_web_search` first" not in "\n".join(
        prompt.extra_context["tool_routing_rules"]
    )


@pytest.mark.asyncio
async def test_search_integration_registers_configured_providers_with_bridge_network(
    tmp_path,
) -> None:
    config = AshConfig(
        workspace=tmp_path,
        models={"default": ModelConfig(provider="openai", model="gpt-5-mini")},
        parallel_search=ParallelSearchConfig(api_key=SecretStr("parallel")),
        exa_search=ExaSearchConfig(enabled=True, api_key=SecretStr("exa")),
        google_places=GooglePlacesConfig(api_key=SecretStr("places")),
    )
    context = _context(config)

    await SearchIntegration().setup(context)

    registry = context.components.tool_registry
    for name in ("web_search", "exa_search", "google_places"):
        assert registry.has(name)
        assert cast(Any, registry.get(name))._executor._config.network_mode == "bridge"

    prompt = SearchIntegration().augment_prompt_context(
        PromptContext(),
        SessionState("s", "cli", "c", "u"),
        context,
    )
    rules = "\n".join(prompt.extra_context["tool_routing_rules"])
    assert "openai_web_search` first" in rules
    assert "`google_places` before general web search" in rules
    assert "Never invoke the `google`" in rules
    assert rules.index("`web_search`, `exa_search`") > rules.index(
        "openai_web_search` first"
    )


@pytest.mark.asyncio
async def test_exa_search_formats_results(monkeypatch, tmp_path) -> None:
    executor = AsyncMock()
    executor.execute.return_value = ExecutionResult(
        exit_code=0,
        stdout=json.dumps(
            {
                "results": [
                    {
                        "title": "Official result",
                        "url": "https://example.com/page",
                        "highlights": ["Relevant excerpt"],
                    }
                ]
            }
        ),
        stderr="",
    )
    executor.cleanup = AsyncMock()
    monkeypatch.setattr(
        "ash.tools.builtin.exa_search.SandboxExecutor", lambda **kwargs: executor
    )
    config = SandboxConfig()
    tool = ExaSearchTool(
        api_key="secret",
        sandbox_config=config,
        workspace_path=tmp_path,
        cache=SearchCache(),
    )

    result = await tool.execute({"query": "test"}, ToolContext())

    assert not result.is_error
    assert "Official result" in result.content
    assert result.metadata["domains"] == ["example.com"]
    assert executor.execute.call_args.kwargs["environment"] == {"EXA_API_KEY": "secret"}
    cached = await tool.execute({"query": "test"}, ToolContext())
    assert cached.metadata["cached"] is True
    assert cached.metadata["domains"] == ["example.com"]
    assert cached.metadata["result_count"] == 1
    assert executor.execute.await_count == 1


@pytest.mark.asyncio
async def test_google_places_formats_current_hours(monkeypatch, tmp_path) -> None:
    executor = AsyncMock()
    executor.execute.return_value = ExecutionResult(
        exit_code=0,
        stdout=json.dumps(
            {
                "places": [
                    {
                        "displayName": {"text": "Sports Basement Bryant"},
                        "formattedAddress": "1590 Bryant St, San Francisco, CA",
                        "businessStatus": "OPERATIONAL",
                        "internationalPhoneNumber": "+14155551212",
                        "currentOpeningHours": {
                            "openNow": True,
                            "weekdayDescriptions": ["Friday: 9:00 AM–8:00 PM"],
                        },
                        "googleMapsUri": "https://maps.google.com/example",
                    }
                ]
            }
        ),
        stderr="",
    )
    executor.cleanup = AsyncMock()
    monkeypatch.setattr(
        "ash.tools.builtin.google_places.SandboxExecutor", lambda **kwargs: executor
    )
    tool = GooglePlacesTool(
        api_key="secret",
        sandbox_config=SandboxConfig(),
        workspace_path=tmp_path,
        cache=SearchCache(),
    )

    result = await tool.execute(
        {"query": "Sports Basement Bryant San Francisco"}, ToolContext()
    )

    assert not result.is_error
    assert "Open now: True" in result.content
    assert "Friday: 9:00 AM–8:00 PM" in result.content
    assert "+14155551212" in result.content
    assert result.metadata["domains"] == ["maps.google.com"]
    assert executor.execute.call_args.kwargs["environment"] == {
        "GOOGLE_MAPS_API_KEY": "secret"
    }
    cached = await tool.execute(
        {"query": "Sports Basement Bryant San Francisco"}, ToolContext()
    )
    assert cached.metadata["cached"] is True
    assert cached.metadata["domains"] == ["maps.google.com"]
    assert cached.metadata["result_count"] == 1
    assert executor.execute.await_count == 1
