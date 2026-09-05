"""Tests for the coding harness foundation."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ash.agents.builtin import register_builtin_agents
from ash.coding import CodingJobStore
from ash.config.models import AshConfig, ModelConfig
from ash.llm.openai import OpenAIProvider
from ash.llm.types import Message, Role, ToolDefinition
from ash.providers.base import IncomingMessage
from ash.providers.telegram.handlers.message_handler import (
    TelegramMessageHandler,
    _unwrap_direct_agent_output,
)
from ash.tools.base import ToolResult
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
    job.project_name = "demo"
    job.changed_files = ["app.py"]
    job.last_pr_url = "https://github.com/example/repo/pull/1"
    job.last_test_result = "Exit code: 0"
    store.save(job)

    loaded = store.get(job.id)

    assert loaded is not None
    assert loaded.id == job.id
    assert loaded.task == "fix the tests"
    assert loaded.status == "testing"
    assert loaded.project_name == "demo"
    assert loaded.changed_files == ["app.py"]
    assert loaded.last_pr_url == "https://github.com/example/repo/pull/1"
    assert loaded.last_test_result == "Exit code: 0"
    assert (
        store.latest_for_chat(chat_id="c1", user_id="u1", provider="telegram").id
        == job.id
    )


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
async def test_telegram_do_command_dispatches_conduit_directly():
    handler = TelegramMessageHandler.__new__(TelegramMessageHandler)
    handler._run_checkpoint_agent_command = AsyncMock()

    message = IncomingMessage(
        id="m1",
        chat_id="c1",
        user_id="u1",
        text="/do find a restaurant and ask about walk-ins",
    )

    result = await handler._try_handle_conduit_command(message)

    assert result is True
    handler._run_checkpoint_agent_command.assert_awaited_once_with(
        message=message,
        task="find a restaurant and ask about walk-ins",
        agent_name="conduit",
        tool_use_prefix="conduit",
        unavailable_label="Conduit agent",
    )


def test_direct_agent_output_unwraps_internal_subagent_envelope():
    wrapped = """<instruction>
This is the result from the \"conduit\" agent.
The user has NOT seen this output.
</instruction>
<output>
can’t do that lookup rn

1) paste the phone #
</output>"""

    assert (
        _unwrap_direct_agent_output(wrapped)
        == "can’t do that lookup rn\n\n1) paste the phone #"
    )


def test_telegram_raw_command_snapshot_survives_in_place_preprocessing():
    raw = IncomingMessage(
        id="m1",
        chat_id="c1",
        user_id="u1",
        text="/code fix failing tests",
        metadata={"thread_id": "t1"},
    )
    snapshot = replace(raw)
    raw.text = "--- injected context ---\n/code fix failing tests"

    handler = TelegramMessageHandler.__new__(TelegramMessageHandler)
    handler._provider = SimpleNamespace(bot_username=None)
    command_message = handler._message_for_raw_slash_command(
        raw_message=snapshot,
        processed_message=raw,
        commands={"/code"},
    )

    assert command_message.text == "/code fix failing tests"
    assert raw.text.startswith("--- injected context ---")


def test_telegram_coding_command_uses_raw_text_after_preprocessing():
    handler = TelegramMessageHandler.__new__(TelegramMessageHandler)
    handler._provider = SimpleNamespace(bot_username=None)

    raw = IncomingMessage(
        id="m1",
        chat_id="c1",
        user_id="u1",
        text="/code fix failing tests",
        metadata={"thread_id": "t1"},
    )
    processed = IncomingMessage(
        id="m1",
        chat_id="c1",
        user_id="u1",
        text="--- injected context ---\n/code fix failing tests",
        metadata={"thread_id": "t1", "source": "preprocessed"},
    )

    command_message = handler._message_for_raw_slash_command(
        raw_message=raw,
        processed_message=processed,
        commands={"/code"},
    )

    assert command_message.text == "/code fix failing tests"
    assert command_message.metadata == processed.metadata


@pytest.mark.asyncio
async def test_telegram_plain_call_request_auto_routes_to_conduit():
    handler = TelegramMessageHandler.__new__(TelegramMessageHandler)
    handler._run_checkpoint_agent_command = AsyncMock()

    message = IncomingMessage(
        id="m1",
        chat_id="c1",
        user_id="u1",
        text="Call the Ace Hardware on 25th and Geary to see if they're still open.",
    )

    result = await handler._try_handle_conduit_intent(message)

    assert result is True
    handler._run_checkpoint_agent_command.assert_awaited_once_with(
        message=message,
        task=message.text,
        agent_name="conduit",
        tool_use_prefix="conduit",
        unavailable_label="Conduit agent",
    )


def test_telegram_plain_call_request_detector_is_narrow():
    handler = TelegramMessageHandler.__new__(TelegramMessageHandler)

    assert handler._looks_like_phone_call_request("please phone Standard Plumbing")
    assert handler._looks_like_phone_call_request(
        "ask Ace Hardware by phone if they are open"
    )
    assert not handler._looks_like_phone_call_request(
        "what is the phone number for Ace Hardware?"
    )
    assert not handler._looks_like_phone_call_request(
        "tell me whether Ace Hardware is open"
    )


@pytest.mark.asyncio
async def test_telegram_plain_coding_request_auto_routes(monkeypatch, tmp_path):
    import ash.coding

    store = CodingJobStore(root=tmp_path)
    monkeypatch.setattr(ash.coding, "CodingJobStore", lambda: store)

    handler = TelegramMessageHandler.__new__(TelegramMessageHandler)
    handler._config = SimpleNamespace(
        coding=SimpleNamespace(
            enabled=True,
            auto_route_enabled=True,
            default_repo_path="/workspace",
        )
    )
    handler._provider = SimpleNamespace(name="telegram")
    handler._run_coding_agent_command = AsyncMock()

    message = IncomingMessage(
        id="m1",
        chat_id="c1",
        user_id="u1",
        text="create a new folder called demo-app and initialize git for the project",
    )

    result = await handler._try_handle_coding_intent(message)

    assert result is True
    handler._run_coding_agent_command.assert_awaited_once()
    call = handler._run_coding_agent_command.await_args.kwargs
    assert call["task"] == message.text
    assert store.get(call["job_id"]) is not None


@pytest.mark.asyncio
async def test_telegram_diff_command_runs_repo_tool_directly(monkeypatch, tmp_path):
    import ash.coding

    store = CodingJobStore(root=tmp_path)
    store.create(
        task="fix failing tests",
        repo_path="/workspace/demo",
        chat_id="c1",
        user_id="u1",
        provider="telegram",
    )
    monkeypatch.setattr(ash.coding, "CodingJobStore", lambda: store)

    repo_tool = AsyncMock()
    repo_tool.execute.return_value = ToolResult.success("diff output")
    handler = TelegramMessageHandler.__new__(TelegramMessageHandler)
    handler._config = SimpleNamespace(
        coding=SimpleNamespace(telegram_commands_enabled=True)
    )
    handler._provider = SimpleNamespace(name="telegram")
    handler._tool_registry = MagicMock()
    handler._tool_registry.has.return_value = True
    handler._tool_registry.get.return_value = repo_tool
    handler._session_handler = MagicMock()
    handler._session_handler.get_session_manager.return_value = MagicMock()
    handler._send_direct_result = AsyncMock()

    message = IncomingMessage(id="m1", chat_id="c1", user_id="u1", text="/diff")

    result = await handler._try_handle_coding_command(message)

    assert result is True
    repo_tool.execute.assert_awaited_once()
    assert repo_tool.execute.await_args.args[0] == {
        "action": "diff",
        "repo_path": "/workspace/demo",
    }
    handler._send_direct_result.assert_awaited_once()


def test_telegram_coding_env_prefers_configured_github_token(monkeypatch):
    handler = TelegramMessageHandler.__new__(TelegramMessageHandler)
    handler._config = SimpleNamespace(
        coding=SimpleNamespace(github_token_env="ASH_GH_TOKEN")
    )
    monkeypatch.setenv("ASH_GH_TOKEN", "token-123")

    assert handler._coding_env() == {
        "GH_TOKEN": "token-123",
        "GITHUB_TOKEN": "token-123",
    }


class FakeExecutor:
    def __init__(self):
        self.calls = []

    async def execute(self, command, **kwargs):
        self.calls.append({"command": command, **kwargs})
        return SimpleNamespace(
            exit_code=0,
            stdout="ok",
            stderr="",
            timed_out=False,
            success=True,
            output="ok",
        )


@pytest.mark.asyncio
async def test_repo_tool_create_project_uses_safe_workspace_path():
    executor = FakeExecutor()
    tool = RepoTool(executor)

    result = await tool.execute(
        {
            "action": "create_project",
            "project_name": "demo-app",
            "projects_root": "/workspace/projects",
        },
        SimpleNamespace(env={}),
    )

    assert result.is_error is False
    assert result.metadata["repo_path"] == "/workspace/projects/demo-app"
    assert "mkdir -p /workspace/projects/demo-app" in executor.calls[0]["command"]


@pytest.mark.asyncio
async def test_repo_tool_requires_approval_for_commit_and_pr_create():
    tool = RepoTool(FakeExecutor())

    commit = await tool.execute(
        {"action": "commit", "repo_path": "/workspace", "message": "test"},
        SimpleNamespace(env={}),
    )
    pr = await tool.execute(
        {"action": "pr_create", "repo_path": "/workspace", "title": "test"},
        SimpleNamespace(env={}),
    )

    assert commit.is_error is True
    assert "explicit user approval" in commit.content
    assert pr.is_error is True
    assert "explicit user approval" in pr.content


@pytest.mark.asyncio
async def test_repo_tool_rejects_paths_outside_workspace():
    tool = RepoTool(FakeExecutor())

    result = await tool.execute(
        {"action": "status", "repo_path": "/etc"},
        SimpleNamespace(env={}),
    )

    assert result.is_error is True
    assert "inside /workspace" in result.content


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
