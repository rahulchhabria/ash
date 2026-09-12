from __future__ import annotations

from pathlib import Path

import pytest

from ash.integrations import (
    BrowserIntegration,
    CapabilitiesIntegration,
    CloseGameAlertIntegration,
    ConduitIntegration,
    DeepAgentsIntegration,
    EmailForwardSummaryIntegration,
    ImageIntegration,
    MemoryIntegration,
    ReactiveWorkflowIntegration,
    RuntimeRPCIntegration,
    SchedulingIntegration,
    SearchIntegration,
    TodoIntegration,
    VapiCallsIntegration,
    create_default_integrations,
)


def test_create_default_integrations_chat_includes_memory() -> None:
    result = create_default_integrations(mode="chat")

    assert [type(item) for item in result.contributors] == [
        SearchIntegration,
        ImageIntegration,
        BrowserIntegration,
        CapabilitiesIntegration,
        VapiCallsIntegration,
        ConduitIntegration,
        DeepAgentsIntegration,
        TodoIntegration,
        MemoryIntegration,
        EmailForwardSummaryIntegration,
        CloseGameAlertIntegration,
        ReactiveWorkflowIntegration,
    ]
    assert result.scheduling is None


def test_create_default_integrations_chat_can_disable_memory() -> None:
    result = create_default_integrations(mode="chat", include_memory=False)

    assert [type(item) for item in result.contributors] == [
        SearchIntegration,
        ImageIntegration,
        BrowserIntegration,
        CapabilitiesIntegration,
        VapiCallsIntegration,
        ConduitIntegration,
        DeepAgentsIntegration,
        TodoIntegration,
        EmailForwardSummaryIntegration,
        CloseGameAlertIntegration,
        ReactiveWorkflowIntegration,
    ]
    assert result.scheduling is None


def test_create_default_integrations_chat_can_disable_todo() -> None:
    result = create_default_integrations(mode="chat", include_todo=False)

    assert [type(item) for item in result.contributors] == [
        SearchIntegration,
        ImageIntegration,
        BrowserIntegration,
        CapabilitiesIntegration,
        VapiCallsIntegration,
        ConduitIntegration,
        DeepAgentsIntegration,
        MemoryIntegration,
        EmailForwardSummaryIntegration,
        CloseGameAlertIntegration,
        ReactiveWorkflowIntegration,
    ]
    assert result.scheduling is None


def test_create_default_integrations_eval_uses_graph_dir_by_default() -> None:
    result = create_default_integrations(mode="eval")
    assert isinstance(result.scheduling, SchedulingIntegration)


def test_create_default_integrations_eval_order() -> None:
    result = create_default_integrations(
        mode="eval",
        include_memory=True,
    )

    assert [type(item) for item in result.contributors] == [
        SchedulingIntegration,
        SearchIntegration,
        ImageIntegration,
        BrowserIntegration,
        CapabilitiesIntegration,
        VapiCallsIntegration,
        ConduitIntegration,
        DeepAgentsIntegration,
        TodoIntegration,
        MemoryIntegration,
        EmailForwardSummaryIntegration,
        CloseGameAlertIntegration,
        ReactiveWorkflowIntegration,
    ]
    assert isinstance(result.scheduling, SchedulingIntegration)


def test_create_default_integrations_eval_can_disable_memory() -> None:
    result = create_default_integrations(
        mode="eval",
        include_memory=False,
    )

    assert [type(item) for item in result.contributors] == [
        SchedulingIntegration,
        SearchIntegration,
        ImageIntegration,
        BrowserIntegration,
        CapabilitiesIntegration,
        VapiCallsIntegration,
        ConduitIntegration,
        DeepAgentsIntegration,
        TodoIntegration,
        EmailForwardSummaryIntegration,
        CloseGameAlertIntegration,
        ReactiveWorkflowIntegration,
    ]
    assert isinstance(result.scheduling, SchedulingIntegration)


def test_create_default_integrations_eval_can_disable_todo() -> None:
    result = create_default_integrations(
        mode="eval",
        include_todo=False,
    )

    assert [type(item) for item in result.contributors] == [
        SchedulingIntegration,
        SearchIntegration,
        ImageIntegration,
        BrowserIntegration,
        CapabilitiesIntegration,
        VapiCallsIntegration,
        ConduitIntegration,
        DeepAgentsIntegration,
        MemoryIntegration,
        EmailForwardSummaryIntegration,
        CloseGameAlertIntegration,
        ReactiveWorkflowIntegration,
    ]
    assert isinstance(result.scheduling, SchedulingIntegration)


def test_create_default_integrations_serve_requires_paths() -> None:
    with pytest.raises(ValueError, match="logs_path"):
        create_default_integrations(mode="serve")


def test_create_default_integrations_serve_order() -> None:
    result = create_default_integrations(
        mode="serve",
        include_memory=True,
        logs_path=Path("logs"),
    )

    assert [type(item) for item in result.contributors] == [
        RuntimeRPCIntegration,
        SearchIntegration,
        ImageIntegration,
        BrowserIntegration,
        CapabilitiesIntegration,
        VapiCallsIntegration,
        ConduitIntegration,
        DeepAgentsIntegration,
        TodoIntegration,
        MemoryIntegration,
        EmailForwardSummaryIntegration,
        CloseGameAlertIntegration,
        ReactiveWorkflowIntegration,
        SchedulingIntegration,
    ]
    assert isinstance(result.scheduling, SchedulingIntegration)


def test_create_default_integrations_serve_can_disable_memory() -> None:
    result = create_default_integrations(
        mode="serve",
        include_memory=False,
        logs_path=Path("logs"),
    )

    assert [type(item) for item in result.contributors] == [
        RuntimeRPCIntegration,
        SearchIntegration,
        ImageIntegration,
        BrowserIntegration,
        CapabilitiesIntegration,
        VapiCallsIntegration,
        ConduitIntegration,
        DeepAgentsIntegration,
        TodoIntegration,
        EmailForwardSummaryIntegration,
        CloseGameAlertIntegration,
        ReactiveWorkflowIntegration,
        SchedulingIntegration,
    ]
    assert isinstance(result.scheduling, SchedulingIntegration)


def test_create_default_integrations_serve_can_disable_todo() -> None:
    result = create_default_integrations(
        mode="serve",
        include_todo=False,
        logs_path=Path("logs"),
    )

    assert [type(item) for item in result.contributors] == [
        RuntimeRPCIntegration,
        SearchIntegration,
        ImageIntegration,
        BrowserIntegration,
        CapabilitiesIntegration,
        VapiCallsIntegration,
        ConduitIntegration,
        DeepAgentsIntegration,
        MemoryIntegration,
        EmailForwardSummaryIntegration,
        CloseGameAlertIntegration,
        ReactiveWorkflowIntegration,
        SchedulingIntegration,
    ]
    assert isinstance(result.scheduling, SchedulingIntegration)


def test_create_default_integrations_rejects_unsupported_mode() -> None:
    with pytest.raises(ValueError, match="unsupported integration mode"):
        create_default_integrations(
            mode="bad-mode",  # type: ignore[arg-type]
            include_memory=True,
        )
