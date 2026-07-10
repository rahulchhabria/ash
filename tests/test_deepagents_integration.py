from __future__ import annotations

import pytest

from ash.deepagents.runtime import (
    AshFilesystemBackend,
    DeepAgentsCodeHelper,
    DeepAgentsRunner,
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
