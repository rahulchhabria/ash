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
from ash.providers.telegram.handlers.message_handler import TelegramMessageHandler
from ash.tools.builtin.coding import HostedOpenAITool
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
