from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from ash.config import AshConfig
from ash.config.models import (
    ModelConfig,
    ReactiveWorkflowConfig,
    ReactiveWorkflowRule,
)
from ash.integrations.reactive_workflows import (
    CONTEXT_FOOTER,
    CONTEXT_HEADER,
    ReactiveWorkflowIntegration,
)
from ash.integrations.runtime import IntegrationContext
from ash.providers.base import IncomingMessage


def _config(*, enabled: bool, rules: list[ReactiveWorkflowRule]) -> AshConfig:
    config = AshConfig(
        workspace=Path("tmp-workspace"),
        models={"default": ModelConfig(provider="openai", model="gpt-5-mini")},
    )
    config.reactive_workflows = ReactiveWorkflowConfig(enabled=enabled, rules=rules)
    return config


def _context(config: AshConfig) -> IntegrationContext:
    return IntegrationContext(
        config=config,
        components=cast(Any, SimpleNamespace()),
        mode="serve",
    )


def _message(*, text: str, chat_type: str | None = None) -> IncomingMessage:
    metadata: dict[str, Any] = {}
    if chat_type is not None:
        metadata["chat_type"] = chat_type
    return IncomingMessage(
        id="m-1", chat_id="c-1", user_id="u-1", text=text, metadata=metadata
    )


async def _setup(integration: ReactiveWorkflowIntegration, config: AshConfig):
    context = _context(config)
    await integration.setup(context)
    return context


@pytest.mark.asyncio
async def test_prefix_match_injects_routing_block() -> None:
    integration = ReactiveWorkflowIntegration()
    config = _config(
        enabled=True,
        rules=[ReactiveWorkflowRule(name="r1", match_prefix="/research", agent="deep")],
    )
    context = await _setup(integration, config)

    msg = _message(text="/research the async landscape")
    updated = await integration.preprocess_incoming_message(msg, context)

    assert CONTEXT_HEADER in updated.text
    assert CONTEXT_FOOTER in updated.text
    assert "deep" in updated.text
    assert "/research the async landscape" in updated.text
    assert updated.metadata["reactive_workflow.rule"] == "r1"


@pytest.mark.asyncio
async def test_regex_match_routes_to_skill() -> None:
    integration = ReactiveWorkflowIntegration()
    config = _config(
        enabled=True,
        rules=[
            ReactiveWorkflowRule(
                name="invoice", match_regex="(?i)invoice", skill="triage"
            )
        ],
    )
    context = await _setup(integration, config)

    updated = await integration.preprocess_incoming_message(
        _message(text="Please handle this INVOICE"), context
    )
    assert "triage" in updated.text
    assert updated.metadata["reactive_workflow.rule"] == "invoice"


@pytest.mark.asyncio
async def test_first_match_wins() -> None:
    integration = ReactiveWorkflowIntegration()
    config = _config(
        enabled=True,
        rules=[
            ReactiveWorkflowRule(name="first", match_regex="report", skill="briefing"),
            ReactiveWorkflowRule(name="second", match_regex="report", agent="deep"),
        ],
    )
    context = await _setup(integration, config)

    updated = await integration.preprocess_incoming_message(
        _message(text="weekly report please"), context
    )
    assert updated.metadata["reactive_workflow.rule"] == "first"
    assert "briefing" in updated.text


@pytest.mark.asyncio
async def test_chat_types_gating() -> None:
    integration = ReactiveWorkflowIntegration()
    config = _config(
        enabled=True,
        rules=[
            ReactiveWorkflowRule(
                name="dm-only",
                match_prefix="/x",
                skill="triage",
                chat_types=["private"],
            )
        ],
    )
    context = await _setup(integration, config)

    # allowed
    allowed = await integration.preprocess_incoming_message(
        _message(text="/x go", chat_type="private"), context
    )
    assert allowed.metadata.get("reactive_workflow.rule") == "dm-only"

    # denied (wrong chat type)
    denied = await integration.preprocess_incoming_message(
        _message(text="/x go", chat_type="group"), context
    )
    assert "reactive_workflow.rule" not in denied.metadata

    # unknown chat type fails closed
    unknown = await integration.preprocess_incoming_message(
        _message(text="/x go", chat_type=None), context
    )
    assert "reactive_workflow.rule" not in unknown.metadata


@pytest.mark.asyncio
async def test_noop_when_disabled() -> None:
    integration = ReactiveWorkflowIntegration()
    config = _config(
        enabled=False,
        rules=[ReactiveWorkflowRule(name="r1", match_prefix="/x", skill="triage")],
    )
    context = await _setup(integration, config)

    updated = await integration.preprocess_incoming_message(
        _message(text="/x go"), context
    )
    assert updated.text == "/x go"
    assert "reactive_workflow.rule" not in updated.metadata


@pytest.mark.asyncio
async def test_no_match_returns_unchanged() -> None:
    integration = ReactiveWorkflowIntegration()
    config = _config(
        enabled=True,
        rules=[ReactiveWorkflowRule(name="r1", match_prefix="/x", skill="triage")],
    )
    context = await _setup(integration, config)

    updated = await integration.preprocess_incoming_message(
        _message(text="nothing to see here"), context
    )
    assert updated.text == "nothing to see here"
    assert "reactive_workflow.rule" not in updated.metadata


@pytest.mark.asyncio
async def test_incomplete_rules_raise_validation_error() -> None:
    with pytest.raises(ValueError, match="match_prefix or match_regex"):
        ReactiveWorkflowRule(name="no-matcher", skill="triage")
    with pytest.raises(ValueError, match="skill, agent, or instruction"):
        ReactiveWorkflowRule(name="no-action", match_prefix="/y")


@pytest.mark.asyncio
async def test_invalid_regex_rules_are_skipped() -> None:
    integration = ReactiveWorkflowIntegration()
    config = _config(
        enabled=True,
        rules=[
            ReactiveWorkflowRule(
                name="bad-regex", match_regex="(unclosed", skill="triage"
            ),
        ],
    )
    context = await _setup(integration, config)

    # No valid rules remain -> integration disabled -> passthrough.
    updated = await integration.preprocess_incoming_message(
        _message(text="/y go"), context
    )
    assert updated.text == "/y go"
    assert "reactive_workflow.rule" not in updated.metadata
