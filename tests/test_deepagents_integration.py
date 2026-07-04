from __future__ import annotations

import pytest

from ash.deepagents.runtime import AshFilesystemBackend, DeepAgentsCodeHelper
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
    assert "langch.in/dcode" in text
