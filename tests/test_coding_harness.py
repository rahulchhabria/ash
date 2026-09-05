"""Tests for the coding harness foundation."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ash.agents.builtin import register_builtin_agents
from ash.coding import ActiveCodingProjectStore, CodingJobStore
from ash.config.models import AshConfig, ModelConfig
from ash.llm.openai import OpenAIProvider
from ash.llm.types import Message, Role, ToolDefinition
from ash.providers.base import IncomingMessage
from ash.providers.telegram.handlers.message_handler import TelegramMessageHandler
from ash.tools.base import ToolContext
from ash.tools.builtin.coding import HostedOpenAITool, RepoTool
from ash.tools.registry import ToolRegistry


def test_coding_job_store_round_trips(tmp_path):
    store = CodingJobStore(root=tmp_path)

    job = store.create(
        task="fix the tests",
        repo_path="/workspace",
        chat_id="c1",
        user_id="u1",
        provider="telegram",
    )
    job.status = "testing"
    job.last_test_result = "Exit code: 0"
    store.save(job)

    loaded = store.get(job.id)

    assert loaded is not None
    assert loaded.id == job.id
    assert loaded.task == "fix the tests"
    assert loaded.status == "testing"
    assert loaded.last_test_result == "Exit code: 0"
    assert (
        store.latest_for_chat(chat_id="c1", user_id="u1", provider="telegram").id
        == job.id
    )


def test_active_coding_project_store_round_trips(tmp_path):
    store = ActiveCodingProjectStore(root=tmp_path)

    project = store.set(
        repo_path="/workspace/git/acme/widget",
        repo="acme/widget",
        chat_id="c1",
        user_id="u1",
        provider="telegram",
        thread_id="t1",
    )

    loaded = store.get(chat_id="c1", user_id="u1", provider="telegram", thread_id="t1")

    assert loaded is not None
    assert loaded.repo_path == project.repo_path
    assert loaded.repo == "acme/widget"


def test_builtin_coding_agent_falls_back_without_codex_alias():
    config = AshConfig(
        models={"default": ModelConfig(provider="openai", model="gpt-5.2")}
    )
    registry = type(
        "Registry",
        (),
        {"agents": [], "register": lambda self, a: self.agents.append(a)},
    )()

    register_builtin_agents(registry, config=config)

    coding = next(agent for agent in registry.agents if agent.config.name == "coding")
    assert coding.config.model is None


def test_builtin_coding_agent_uses_configured_codex_alias():
    config = AshConfig(
        models={
            "default": ModelConfig(provider="openai", model="gpt-5.2"),
            "codex": ModelConfig(provider="openai", model="gpt-5.2-codex"),
        }
    )
    registry = type(
        "Registry",
        (),
        {"agents": [], "register": lambda self, a: self.agents.append(a)},
    )()

    register_builtin_agents(registry, config=config)

    coding = next(agent for agent in registry.agents if agent.config.name == "coding")
    assert coding.config.model == "codex"


def test_openai_converts_hosted_tools_and_skips_unconfigured_file_search():
    provider = OpenAIProvider(api_key="test-key")
    messages = [Message(role=Role.USER, content="search docs")]
    tools = [
        ToolDefinition(
            name="openai_web_search",
            description="hosted search",
            input_schema={"type": "object", "properties": {}},
            kind="hosted",
            metadata={
                "openai_tool": {
                    "type": "web_search_preview",
                    "search_context_size": "medium",
                }
            },
        ),
        ToolDefinition(
            name="openai_file_search",
            description="hosted file search",
            input_schema={"type": "object", "properties": {}},
            kind="hosted",
            metadata={"openai_tool": {"type": "file_search", "vector_store_ids": []}},
        ),
    ]

    kwargs = provider._build_request_kwargs(
        messages=messages,
        model="gpt-5.2",
        tools=tools,
        system=None,
        max_tokens=4096,
        temperature=None,
    )

    assert kwargs["tools"] == [
        {"type": "web_search_preview", "search_context_size": "medium"}
    ]


def test_tool_registry_preserves_hosted_tool_definitions():
    registry = ToolRegistry()
    registry.register(
        HostedOpenAITool(
            "openai_web_search",
            "hosted search",
            {"type": "web_search_preview", "search_context_size": "medium"},
        )
    )

    definition = registry.get_definitions()[0]

    assert definition.kind == "hosted"
    assert definition.metadata == {
        "openai_tool": {"type": "web_search_preview", "search_context_size": "medium"}
    }


@pytest.mark.asyncio
async def test_telegram_code_command_dispatches_directly(monkeypatch, tmp_path):
    import ash.coding

    store = CodingJobStore(root=tmp_path)
    monkeypatch.setattr(ash.coding, "CodingJobStore", lambda: store)

    handler = TelegramMessageHandler.__new__(TelegramMessageHandler)
    handler._config = SimpleNamespace(
        coding=SimpleNamespace(
            telegram_commands_enabled=True,
            default_repo_path="/workspace",
        )
    )
    handler._provider = SimpleNamespace(name="telegram")
    handler._run_coding_agent_command = AsyncMock()

    message = IncomingMessage(
        id="m1",
        chat_id="c1",
        user_id="u1",
        text="/code fix failing tests",
    )

    result = await handler._try_handle_coding_command(message)

    assert result is True
    handler._run_coding_agent_command.assert_awaited_once()
    call = handler._run_coding_agent_command.await_args.kwargs
    assert call["task"] == "fix failing tests"
    assert call["repo_path"] == "/workspace"
    assert store.get(call["job_id"]) is not None


@pytest.mark.asyncio
async def test_telegram_open_sets_active_project(monkeypatch, tmp_path):
    import ash.coding

    project_store = ActiveCodingProjectStore(root=tmp_path)
    monkeypatch.setattr(ash.coding, "ActiveCodingProjectStore", lambda: project_store)
    monkeypatch.setattr(
        ash.coding, "CodingJobStore", lambda: CodingJobStore(root=tmp_path / "jobs")
    )

    handler = TelegramMessageHandler.__new__(TelegramMessageHandler)
    handler._config = SimpleNamespace(
        coding=SimpleNamespace(telegram_commands_enabled=True)
    )
    handler._provider = SimpleNamespace(name="telegram")
    handler._send_direct_result = AsyncMock()

    message = IncomingMessage(
        id="m1",
        chat_id="c1",
        user_id="u1",
        text="/open acme/widget",
    )

    result = await handler._try_handle_coding_command(message)

    assert result is True
    project = project_store.get(chat_id="c1", user_id="u1", provider="telegram")
    assert project is not None
    assert project.repo_path == "/workspace/git/acme/widget"
    assert project.repo == "acme/widget"
    handler._send_direct_result.assert_awaited_once()


@pytest.mark.asyncio
async def test_telegram_code_uses_active_project(monkeypatch, tmp_path):
    import ash.coding

    store = CodingJobStore(root=tmp_path / "jobs")
    project_store = ActiveCodingProjectStore(root=tmp_path / "projects")
    project_store.set(
        repo_path="/workspace/git/acme/widget",
        repo="acme/widget",
        chat_id="c1",
        user_id="u1",
        provider="telegram",
    )
    monkeypatch.setattr(ash.coding, "CodingJobStore", lambda: store)
    monkeypatch.setattr(ash.coding, "ActiveCodingProjectStore", lambda: project_store)

    handler = TelegramMessageHandler.__new__(TelegramMessageHandler)
    handler._config = SimpleNamespace(
        coding=SimpleNamespace(telegram_commands_enabled=True)
    )
    handler._provider = SimpleNamespace(name="telegram")
    handler._run_coding_agent_command = AsyncMock()

    message = IncomingMessage(
        id="m1",
        chat_id="c1",
        user_id="u1",
        text="/code fix failing tests",
    )

    result = await handler._try_handle_coding_command(message)

    assert result is True
    call = handler._run_coding_agent_command.await_args.kwargs
    assert call["repo_path"] == "/workspace/git/acme/widget"
    assert store.get(call["job_id"]).repo_path == "/workspace/git/acme/widget"


@pytest.mark.asyncio
async def test_telegram_cancel_clears_persisted_stack(monkeypatch, tmp_path):
    import ash.coding

    store = CodingJobStore(root=tmp_path)
    store.create(
        task="fix failing tests",
        repo_path="/workspace",
        chat_id="c1",
        user_id="u1",
        provider="telegram",
    )
    monkeypatch.setattr(ash.coding, "CodingJobStore", lambda: store)

    session_manager = MagicMock()
    handler = TelegramMessageHandler.__new__(TelegramMessageHandler)
    handler._config = SimpleNamespace(
        coding=SimpleNamespace(telegram_commands_enabled=True)
    )
    handler._provider = SimpleNamespace(name="telegram")
    handler._stack_manager = MagicMock()
    handler._session_handler = MagicMock()
    handler._session_handler.get_session_manager.return_value = session_manager
    handler._send_direct_result = AsyncMock()

    message = IncomingMessage(
        id="m1",
        chat_id="c1",
        user_id="u1",
        text="/cancel",
    )

    result = await handler._try_handle_coding_command(message)

    assert result is True
    handler._stack_manager.clear.assert_called_once()
    session_manager.save_active_stack.assert_called_once_with(None)
    assert (
        store.latest_for_chat(chat_id="c1", user_id="u1", provider="telegram") is None
    )


class _FakeSandboxExecutor:
    def __init__(self):
        self.commands = []

    async def execute(self, command, **kwargs):
        self.commands.append((command, kwargs))
        return SimpleNamespace(
            output="ok",
            success=True,
            exit_code=0,
            timed_out=False,
        )


@pytest.mark.asyncio
async def test_repo_tool_runs_in_requested_repo_path():
    executor = _FakeSandboxExecutor()
    tool = RepoTool(executor)  # type: ignore[arg-type]

    result = await tool.execute(
        {"action": "status", "repo_path": "/workspace/git/acme/widget"},
        ToolContext(),
    )

    assert not result.is_error
    assert executor.commands[0][0].startswith("cd /workspace/git/acme/widget &&")
    assert "Repo path: /workspace/git/acme/widget" in result.content


@pytest.mark.asyncio
async def test_repo_tool_supports_merge_push_and_pr_create():
    executor = _FakeSandboxExecutor()
    tool = RepoTool(executor)  # type: ignore[arg-type]

    await tool.execute({"action": "merge", "branch": "feature/x"}, ToolContext())
    await tool.execute({"action": "push"}, ToolContext())
    await tool.execute({"action": "pr_create", "base": "main"}, ToolContext())

    commands = [call[0] for call in executor.commands]
    assert "git merge --no-ff feature/x" in commands[0]
    assert "git push -u origin HEAD" in commands[1]
    assert "gh pr create --base main --fill" in commands[2]
