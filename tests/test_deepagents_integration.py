from __future__ import annotations

import pytest

from ash.config import AshConfig
from ash.config.models import ModelConfig
from ash.deepagents.runtime import (
    AshFilesystemBackend,
    DeepAgentsCodeHelper,
    DeepAgentsRunner,
    build_default_orchestration_subagents,
)
from ash.tools.base import ToolContext
from ash.tools.builtin.deepagents import DeepAgentsStatusTool, DeepResearchTool


@pytest.mark.asyncio
async def test_deepagents_status_tool_reports_optional_dependency() -> None:
    result = await DeepAgentsStatusTool().execute({}, ToolContext())
    assert not result.is_error
    assert "deepagents_installed" in result.content
    assert "Deep Agents Code" in result.content


@pytest.mark.asyncio
async def test_deep_research_missing_task() -> None:
    result = await DeepResearchTool().execute({}, ToolContext())
    assert result.is_error
    assert "task" in result.content


@pytest.mark.asyncio
async def test_ash_filesystem_backend_rejects_escape(tmp_path) -> None:
    backend = AshFilesystemBackend(root=tmp_path)
    await backend.write_file("notes/a.txt", "hello")
    assert await backend.read_file("notes/a.txt") == "hello"
    with pytest.raises(ValueError):
        await backend.write_file("../escape.txt", "nope")


def test_deep_agents_code_helper_mentions_workspace(tmp_path) -> None:
    text = DeepAgentsCodeHelper(workspace=tmp_path).instructions()
    assert str(tmp_path) in text
    assert "docs.langchain.com/deep-agents" in text
    assert "do not pipe" in text


@pytest.mark.asyncio
async def test_deepagents_backend_uses_virtual_workspace_paths(tmp_path) -> None:
    backend = AshFilesystemBackend(root=tmp_path)
    await backend.write_file("/notes/a.txt", "alpha\nbeta\n")

    assert await backend.read_file("/notes/a.txt") == "alpha\nbeta\n"
    assert await backend.list_files("/") == ["notes/a.txt"]
    matches = await backend.search_matches("beta", "/")
    assert matches == [{"path": "/notes/a.txt", "line": 2, "text": "beta"}]


def test_deepagents_runner_create_uses_workspace_backend(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_create_deep_agent(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        "ash.deepagents.runtime.require_deepagents",
        lambda: fake_create_deep_agent,
    )

    DeepAgentsRunner(workspace_path=tmp_path).create()

    assert "backend" in captured
    assert captured["model"] == "openai:gpt-5.1"
    assert captured["backend"]._backend.read_only is True
    assert [item["name"] for item in captured["subagents"]] == [
        "general-purpose",
        "researcher",
        "planner",
    ]


def test_deepagents_runner_can_use_read_write_backend(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, object] = {}

    def fake_create_deep_agent(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(
        "ash.deepagents.runtime.require_deepagents",
        lambda: fake_create_deep_agent,
    )

    DeepAgentsRunner(workspace_path=tmp_path, filesystem_mode="read_write").create()

    assert captured["backend"]._backend.read_only is False


def test_default_orchestration_subagents_are_minimal() -> None:
    tools = [lambda: "ok"]

    subagents = build_default_orchestration_subagents(
        model="openai:gpt-5.1",
        tools=tools,
    )

    assert {item["name"] for item in subagents} == {
        "general-purpose",
        "researcher",
        "planner",
    }
    assert subagents[0]["tools"] == tools
    assert subagents[2]["tools"] == []


@pytest.mark.asyncio
async def test_deep_research_respects_disabled_config() -> None:
    config = AshConfig(
        workspace="tmp-workspace",
        models={"default": ModelConfig(provider="openai", model="gpt-5-mini")},
    )
    config.deepagents.enabled = False

    result = await DeepResearchTool(config=config).execute(
        {"task": "research this"},
        ToolContext(),
    )

    assert result.is_error
    assert "disabled" in result.content


@pytest.mark.asyncio
async def test_deep_research_uses_configured_tool_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_ainvoke(self, message: str) -> str:
        captured["message"] = message
        captured["tool_names"] = [tool.__name__ for tool in self.tools]
        captured["filesystem_mode"] = self.filesystem_mode
        captured["builtin_subagents"] = self.builtin_subagents
        return "done"

    class FakeExecutor:
        available_tools = ["read_file", "write_file", "bash"]

    config = AshConfig(
        workspace="tmp-workspace",
        models={"default": ModelConfig(provider="openai", model="gpt-5-mini")},
    )
    config.deepagents.allowed_tools = ["read_file"]

    monkeypatch.setattr(
        "ash.tools.builtin.deepagents.DeepAgentsRunner.ainvoke",
        fake_ainvoke,
    )

    result = await DeepResearchTool(
        tool_executor=FakeExecutor(),
        config=config,
    ).execute({"task": "x" * 20}, ToolContext())

    assert not result.is_error
    assert result.content == "done"
    assert captured["tool_names"] == ["ash_read_file"]
    assert captured["filesystem_mode"] == "read_only"
    assert captured["builtin_subagents"] is True
