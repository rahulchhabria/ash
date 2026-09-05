"""Telegram message handling utilities."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ash.agents.types import ChildActivated
from ash.config.models import ConversationConfig
from ash.core import Agent
from ash.core.signals import is_no_reply
from ash.providers.base import IncomingMessage, OutgoingMessage
from ash.providers.telegram.handlers.checkpoint_handler import CheckpointHandler
from ash.providers.telegram.handlers.passive_handler import PassiveHandler
from ash.providers.telegram.handlers.provenance import (
    build_provenance_clause_from_tool_calls,
)
from ash.providers.telegram.handlers.session_handler import (
    SessionHandler,
    SessionLock,
)
from ash.providers.telegram.handlers.tool_tracker import (
    ProgressMessageTool,
    ToolTracker,
)
from ash.providers.telegram.handlers.utils import append_inline_attribution
from ash.providers.telegram.provider import _truncate, is_stale_callback_error
from ash.sessions.types import session_key as make_session_key
from ash.tools.base import ToolContext

if TYPE_CHECKING:
    from aiogram.types import CallbackQuery

    from ash.agents import AgentExecutor, AgentRegistry
    from ash.config import AshConfig
    from ash.core import SessionState
    from ash.llm import LLMProvider
    from ash.memory.extractor import MemoryExtractor
    from ash.providers.telegram.provider import TelegramProvider
    from ash.skills import SkillRegistry
    from ash.store.store import Store
    from ash.tools.registry import ToolRegistry

logger = logging.getLogger("telegram")
_LOCALHOST_CALLBACK_URL_PATTERN = re.compile(r"https?://localhost[^\s]*[?&]code=[^\s]*")
_SUBAGENT_OUTPUT_PATTERN = re.compile(
    r"^\s*<instruction>.*?</instruction>\s*<output>\s*(.*?)\s*</output>\s*$",
    re.DOTALL,
)


def _unwrap_direct_agent_output(text: str) -> str:
    match = _SUBAGENT_OUTPUT_PATTERN.match(text)
    return match.group(1).strip() if match else text


def _extract_tool_calls_from_session(session: SessionState) -> list[dict[str, Any]]:
    from ash.llm.types import ToolResult as LLMToolResult
    from ash.llm.types import ToolUse

    ordered_calls: list[dict[str, Any]] = []
    tool_results_by_id: dict[str, tuple[str, bool]] = {}
    for msg in session.messages:
        content = msg.content
        if isinstance(content, str):
            continue
        for block in content:
            if isinstance(block, ToolUse):
                ordered_calls.append(
                    {
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                )
            elif isinstance(block, LLMToolResult):
                tool_results_by_id[block.tool_use_id] = (block.content, block.is_error)

    tool_calls: list[dict[str, Any]] = []
    for call in ordered_calls:
        output, is_error = tool_results_by_id.get(call["id"], ("", False))
        tool_calls.append(
            {
                "id": call["id"],
                "name": call["name"],
                "input": call["input"],
                "result": output,
                "is_error": is_error,
            }
        )
    return tool_calls


class TelegramMessageHandler:
    """Handler that connects Telegram messages to the agent."""

    def __init__(
        self,
        provider: TelegramProvider,
        agent: Agent,
        store: Store | None = None,
        streaming: bool = False,
        conversation_config: ConversationConfig | None = None,
        config: AshConfig | None = None,
        agent_registry: AgentRegistry | None = None,
        skill_registry: SkillRegistry | None = None,
        tool_registry: ToolRegistry | None = None,
        llm_provider: LLMProvider | None = None,
        memory_manager: Store | None = None,
        memory_extractor: MemoryExtractor | None = None,
        agent_executor: AgentExecutor | None = None,
    ):
        self._provider = provider
        self._agent = agent
        self._store = store
        self._streaming = streaming
        self._conversation_config = conversation_config or ConversationConfig()
        self._config = config
        self._agent_registry = agent_registry
        self._skill_registry = skill_registry
        self._tool_registry = tool_registry
        self._llm_provider = llm_provider
        self._memory_manager = memory_manager
        self._memory_extractor = memory_extractor
        self._agent_executor = agent_executor
        max_concurrent = config.sessions.max_concurrent if config else 2
        self._concurrency_semaphore = asyncio.Semaphore(max_concurrent)

        # Stack manager for interactive subagent sessions
        from ash.agents.types import AgentStackManager

        self._stack_manager = AgentStackManager()

        # Session handler for session lifecycle and persistence
        self._session_handler = SessionHandler(
            provider_name=provider.name,
            config=config,
            conversation_config=self._conversation_config,
            store=store,
            bot_username=provider.bot_username,
        )

        # Checkpoint handler for inline keyboard callbacks
        self._checkpoint_handler = CheckpointHandler(
            provider=provider,
            get_session_manager=self._session_handler.get_session_manager,
            get_session_managers_dict=lambda: self._session_handler._session_managers,
            get_thread_index=self._session_handler.get_thread_index,
            handle_message=self.handle_message,
            mark_active_thread=lambda chat_id, thread_id: (
                self._session_handler.mark_active_thread(
                    chat_id, thread_id, reason="pending_checkpoint"
                )
            ),
            config=config,
            agent_registry=agent_registry,
            skill_registry=skill_registry,
            tool_registry=tool_registry,
        )

        # Streaming handler for streaming responses
        from ash.providers.telegram.handlers.streaming_handler import StreamingHandler

        self._streaming_handler = StreamingHandler(
            provider=provider,
            agent=agent,
            session_handler=self._session_handler,
            create_tool_tracker=self._create_tool_tracker,
            log_response=self._log_response,
        )

        # Sync handler for non-streaming responses
        from ash.providers.telegram.handlers.sync_handler import SyncHandler

        self._sync_handler = SyncHandler(
            provider=provider,
            agent=agent,
            session_handler=self._session_handler,
            create_tool_tracker=self._create_tool_tracker,
            log_response=self._log_response,
            store_checkpoint=self._store_checkpoint,
        )

        # Register provider-specific tools
        self._register_provider_tools()

        # Initialize passive handler if configured
        self._passive_handler: PassiveHandler | None = None
        if provider.passive_config and provider.passive_config.enabled:
            self._passive_handler = PassiveHandler(
                provider=provider,
                config=config,
                llm_provider=llm_provider,
                memory_manager=memory_manager,
                memory_extractor=memory_extractor,
                handle_message=self.handle_message,
            )

    def _register_provider_tools(self) -> None:
        """Register provider-specific tools that need access to the provider."""
        if self._tool_registry is None:
            return

        from ash.tools.builtin.messages import SendMessageTool

        if not self._tool_registry.has("send_message"):
            send_message_tool = SendMessageTool(
                provider=self._provider,
                session_manager_factory=self._session_handler.get_session_manager,
                thread_index_factory=self._session_handler.get_thread_index,
            )
            self._tool_registry.register(send_message_tool)
            logger.debug("Registered send_message tool for Telegram provider")

    async def handle_passive_message(self, message: IncomingMessage) -> None:
        """Handle a passively observed message (not mentioned or replied to).

        Delegates to PassiveHandler if passive listening is enabled.
        """
        if self._passive_handler:
            await self._passive_handler.handle_passive_message(message)

    def _create_tool_tracker(self, message: IncomingMessage) -> ToolTracker:
        return ToolTracker(
            provider=self._provider,
            chat_id=message.chat_id,
            reply_to=message.id,
            config=self._config,
            agent_registry=self._agent_registry,
            skill_registry=self._skill_registry,
        )

    def _get_capability_manager(self) -> Any | None:
        """Best-effort lookup of the capability manager via use_skill tool wiring."""
        if self._tool_registry is None:
            return None
        if not hasattr(self._tool_registry, "has") or not self._tool_registry.has(
            "use_skill"
        ):
            return None
        try:
            skill_tool = self._tool_registry.get("use_skill")
        except Exception:
            return None
        return getattr(skill_tool, "_capability_manager", None)

    @staticmethod
    def _extract_localhost_callback_url(message_text: str) -> str | None:
        """Extract a localhost OAuth callback URL from message text."""
        match = _LOCALHOST_CALLBACK_URL_PATTERN.search(message_text)
        if not match:
            return None
        callback_url = match.group(0).strip().rstrip(".,;")
        return callback_url or None

    def _parse_slash_command(self, message_text: str) -> tuple[str, str] | None:
        """Parse an exact slash command and its trailing arguments."""
        text = (message_text or "").strip()
        if not text.startswith("/"):
            return None

        first, _, remainder = text.partition(" ")
        command = first.strip().lower()
        if not command:
            return None

        if "@" in command:
            base, _, mention = command.partition("@")
            bot_username = (self._provider.bot_username or "").strip().lower()
            if mention and bot_username and mention != bot_username:
                return None
            command = base

        return command, remainder.strip()

    def _message_for_raw_slash_command(
        self,
        *,
        raw_message: IncomingMessage,
        processed_message: IncomingMessage,
        commands: set[str],
    ) -> IncomingMessage:
        parsed = self._parse_slash_command(raw_message.text or "")
        if parsed is None or parsed[0] not in commands:
            return processed_message
        if raw_message.text == processed_message.text:
            return processed_message
        return replace(processed_message, text=raw_message.text)

    def _match_triggered_skill(
        self,
        message_text: str,
    ) -> tuple[Any, str, str] | None:
        """Resolve a slash-command trigger to a skill plus argument text."""
        if self._skill_registry is None:
            return None
        parsed = self._parse_slash_command(message_text)
        if parsed is None:
            return None
        command, arguments = parsed
        skill = self._skill_registry.find_by_trigger(command)
        if skill is None:
            return None
        return skill, command, arguments

    async def _send_direct_result(
        self,
        *,
        message: IncomingMessage,
        assistant_text: str,
        skip_user_message: bool = False,
    ) -> str:
        """Send a direct Telegram reply and persist it."""
        response_external_id = await self._provider.send(
            OutgoingMessage(
                chat_id=message.chat_id,
                text=assistant_text,
                reply_to_message_id=message.id,
            )
        )
        self._log_response(assistant_text)
        await self._session_handler.persist_messages(
            chat_id=message.chat_id,
            user_id=message.user_id,
            user_message=message.text,
            assistant_message=assistant_text,
            external_id=message.id,
            response_external_id=response_external_id,
            thread_id=message.metadata.get("thread_id"),
            username=message.username,
            display_name=message.display_name,
            reply_to_external_id=message.reply_to_message_id,
            skip_user_message=skip_user_message,
        )
        return response_external_id

    async def _try_handle_triggered_skill(self, message: IncomingMessage) -> bool:
        """Handle explicit slash-command skill triggers without main-agent routing."""
        thread_id = message.metadata.get("thread_id")
        session_key = make_session_key(
            self._provider.name,
            message.chat_id,
            message.user_id,
            thread_id,
        )
        if self._stack_manager.has_active(session_key):
            return False

        matched = self._match_triggered_skill(message.text or "")
        if matched is None:
            return False
        if self._tool_registry is None or not self._tool_registry.has("use_skill"):
            return False

        skill, command, arguments = matched
        if not arguments:
            await self._send_direct_result(
                message=message,
                assistant_text=(
                    f"{command} requires a research task.\n\n"
                    f"Example: `{command} compare Sentry and Honeycomb for AI debugging`"
                ),
            )
            return True

        use_skill_tool = self._tool_registry.get("use_skill")
        tracker = self._create_tool_tracker(message)

        session = await self._session_handler.get_or_create_session(message)
        if session.has_incomplete_tool_use():
            session.repair_incomplete_tool_use()

        session_manager = self._session_handler.get_session_manager(
            message.chat_id,
            message.user_id,
            thread_id,
        )
        user_metadata: dict[str, str] = {}
        if message.id:
            user_metadata["external_id"] = message.id
        if message.reply_to_message_id:
            user_metadata["reply_to_external_id"] = message.reply_to_message_id
        await session_manager.add_user_message(
            content=message.text,
            metadata=user_metadata or None,
            username=message.username,
            display_name=message.display_name,
            user_id=message.user_id,
        )

        from ash.providers.telegram.handlers.tool_tracker import ProgressMessageTool

        progress_tool = ProgressMessageTool(tracker)
        tool_context = ToolContext(
            session_id=session.session_id,
            user_id=message.user_id,
            chat_id=message.chat_id,
            thread_id=thread_id,
            provider=self._provider.name,
            metadata={
                **message.metadata,
                "reply_to_message_id": message.id,
                "current_user_message": message.text,
            },
            session_manager=session_manager,
            tool_use_id=f"trigger-skill-{message.id}",
            tool_overrides={progress_tool.name: progress_tool},
        )

        logger.info(
            "triggered_skill_invoked",
            extra={
                "skill": skill.name,
                "command": command,
                "chat_id": message.chat_id,
            },
        )

        try:
            result = await use_skill_tool.execute(
                {
                    "skill": skill.name,
                    "message": arguments,
                    "context": (
                        f"Invoked by explicit slash command `{command}`.\n"
                        "Treat the slash command itself as authoritative routing input."
                    ),
                },
                tool_context,
            )
        except ChildActivated as ca:
            if not self._agent_executor or not ca.child_frame:
                await self._send_direct_result(
                    message=message,
                    assistant_text="Triggered skill could not be started.",
                    skip_user_message=True,
                )
                return True

            stack = self._stack_manager.get_or_create(session_key)
            stack.push(ca.child_frame)
            self._persist_stack(session_key, session_manager)
            response_external_id = await self._run_orchestration_loop(
                message,
                session_key,
                entry_user_message=None,
                thinking_msg_id=tracker.thinking_msg_id,
                tracker=tracker,
            )
            await self._session_handler.persist_messages(
                chat_id=message.chat_id,
                user_id=message.user_id,
                user_message=message.text,
                assistant_message=None,
                external_id=message.id,
                response_external_id=response_external_id,
                thread_id=thread_id,
                username=message.username,
                display_name=message.display_name,
                reply_to_external_id=message.reply_to_message_id,
                skip_user_message=True,
            )
            return True

        await self._send_direct_result(
            message=message,
            assistant_text=result.content,
            skip_user_message=True,
        )
        return True

    def _coding_env(self) -> dict[str, str]:
        coding_config = getattr(self._config, "coding", None)
        token_env = getattr(coding_config, "github_token_env", "GH_TOKEN") or "GH_TOKEN"
        env: dict[str, str] = {}
        for name in dict.fromkeys([str(token_env), "GH_TOKEN", "GITHUB_TOKEN"]):
            token = os.environ.get(name)
            if token:
                env["GH_TOKEN"] = token
                env["GITHUB_TOKEN"] = token
                break
        return env

    def _looks_like_phone_call_request(self, message_text: str) -> bool:
        text = (message_text or "").strip().lower()
        if not text or text.startswith("/"):
            return False

        if re.search(r"\b(phone number|number for|what'?s the number)\b", text):
            return False

        direct_call_verbs = ("call", "ring", "dial")
        if any(
            re.search(rf"\b{re.escape(verb)}\b", text) for verb in direct_call_verbs
        ):
            return True

        if re.search(r"\b(phone|place a call|make a call)\b.+\b(to|for|with)\b", text):
            return True
        if re.search(r"\b(phone)\b\s+[^?]+$", text):
            return True

        return bool(
            re.search(r"\bask\b.+\b(by phone|over the phone|on the phone)\b", text)
        )

    def _looks_like_coding_request(self, message_text: str) -> bool:
        text = (message_text or "").strip().lower()
        if not text or text.startswith("/"):
            return False
        action = (
            "build",
            "create",
            "make",
            "implement",
            "fix",
            "debug",
            "refactor",
            "add",
            "write",
            "run tests",
            "open a pr",
            "commit",
            "clone",
            "initialize git",
            "init git",
        )
        target = (
            "app",
            "site",
            "repo",
            "repository",
            "project",
            "folder",
            "directory",
            "code",
            "test",
            "branch",
            "pull request",
            "pr",
            "github",
            "file",
        )
        return any(word in text for word in action) and any(
            word in text for word in target
        )

    def _format_coding_status(self, job: Any) -> str:
        changed = ", ".join(job.changed_files) if job.changed_files else "(none)"
        return (
            f"Coding job: {job.id}\n"
            f"Status: {job.status}\n"
            f"Repo: {job.repo_path}\n"
            f"Project: {job.project_name or '(not set)'}\n"
            f"Branch: {job.branch or '(not set)'}\n"
            f"Task: {job.task}\n"
            f"Changed files: {changed}\n"
            f"Last diff: {job.last_diff_summary or '(none)'}\n"
            f"Last test: {job.last_test_result or '(none)'}\n"
            f"PR: {job.last_pr_url or '(none)'}"
        )

    async def _execute_repo_for_job(
        self,
        *,
        message: IncomingMessage,
        job: Any,
        action: str,
        command: str | None = None,
    ) -> str:
        if self._tool_registry is None or not self._tool_registry.has("repo"):
            return "Repo tool is not available in this runtime."
        repo_tool = self._tool_registry.get("repo")
        thread_id = message.metadata.get("thread_id")
        session_manager = self._session_handler.get_session_manager(
            message.chat_id, message.user_id, thread_id
        )
        tool_context = ToolContext(
            session_id=make_session_key(
                self._provider.name, message.chat_id, message.user_id, thread_id
            ),
            user_id=message.user_id,
            chat_id=message.chat_id,
            thread_id=thread_id,
            provider=self._provider.name,
            metadata={"message_id": message.id, "reply_to_message_id": message.id},
            session_manager=session_manager,
            tool_use_id=f"repo-{action}-{message.id}",
            env=self._coding_env(),
        )
        input_data: dict[str, Any] = {"action": action, "repo_path": job.repo_path}
        if command:
            input_data["command"] = command
        result = await repo_tool.execute(input_data, tool_context)
        from ash.coding import CodingJobStore

        store = CodingJobStore()
        if action == "test":
            job.last_test_command = command
            job.last_test_result = result.content[:1000]
        elif action == "diff":
            job.last_diff_summary = result.content[:1000]
        job.status = "ready" if not result.is_error else "failed"
        store.save(job)
        return result.content

    async def _try_handle_coding_intent(self, message: IncomingMessage) -> bool:
        coding_config = getattr(self._config, "coding", None)
        if coding_config is not None:
            if not getattr(coding_config, "enabled", True):
                return False
            if not getattr(coding_config, "auto_route_enabled", True):
                return False
        if not self._looks_like_coding_request(message.text or ""):
            return False
        from ash.coding import CodingJobStore

        repo_path = getattr(coding_config, "default_repo_path", "/workspace")
        task = message.text.strip()
        store = CodingJobStore()
        job = store.create(
            task=task,
            repo_path=repo_path,
            chat_id=message.chat_id,
            user_id=message.user_id,
            provider=self._provider.name,
            thread_id=message.metadata.get("thread_id"),
            telegram_message_id=message.id,
        )
        await self._run_coding_agent_command(
            message=message,
            task=task,
            job_id=job.id,
            repo_path=repo_path,
        )
        return True

    async def _try_handle_coding_command(
        self, message: IncomingMessage
    ) -> IncomingMessage | bool:
        """Handle Telegram coding harness slash commands.

        Returns a rewritten message for commands that should enter the normal
        agent loop, True when handled directly, or False when not a coding command.
        """
        coding_config = getattr(self._config, "coding", None)
        if coding_config is not None and not getattr(
            coding_config, "telegram_commands_enabled", True
        ):
            return False

        parsed = self._parse_slash_command(message.text)
        if parsed is None:
            return False
        command, arguments = parsed
        if command not in {"/code", "/status", "/diff", "/test", "/cancel"}:
            return False

        from ash.coding import CodingJobStore

        store = CodingJobStore()
        repo_path = getattr(coding_config, "default_repo_path", "/workspace")

        if command == "/code":
            task = arguments.strip()
            if not task:
                await self._send_direct_result(
                    message=message,
                    assistant_text="Usage: /code <coding task>",
                    skip_user_message=False,
                )
                return True
            job = store.create(
                task=task,
                repo_path=repo_path,
                chat_id=message.chat_id,
                user_id=message.user_id,
                provider=self._provider.name,
                thread_id=message.metadata.get("thread_id"),
                telegram_message_id=message.id,
            )
            await self._run_coding_agent_command(
                message=message,
                task=task,
                job_id=job.id,
                repo_path=repo_path,
            )
            return True

        job = store.latest_for_chat(
            chat_id=message.chat_id,
            user_id=message.user_id,
            provider=self._provider.name,
        )
        if job is None:
            await self._send_direct_result(
                message=message,
                assistant_text="No active coding job for this chat.",
                skip_user_message=False,
            )
            return True

        if command == "/status":
            await self._send_direct_result(
                message=message,
                assistant_text=self._format_coding_status(job),
                skip_user_message=False,
            )
            return True

        if command == "/cancel":
            job.status = "cancelled"
            store.save(job)
            thread_id = message.metadata.get("thread_id")
            session_key = make_session_key(
                self._provider.name,
                message.chat_id,
                message.user_id,
                thread_id,
            )
            self._stack_manager.clear(session_key)
            session_manager = self._session_handler.get_session_manager(
                message.chat_id, message.user_id, thread_id
            )
            session_manager.save_active_stack(None)
            await self._send_direct_result(
                message=message,
                assistant_text=f"Cancelled coding job {job.id}.",
                skip_user_message=False,
            )
            return True

        if command == "/diff":
            output = await self._execute_repo_for_job(
                message=message,
                job=job,
                action="diff",
            )
            await self._send_direct_result(
                message=message,
                assistant_text=output,
                skip_user_message=False,
            )
            return True

        if command == "/test":
            test_command = arguments.strip() or job.last_test_command or "make test"
            output = await self._execute_repo_for_job(
                message=message,
                job=job,
                action="test",
                command=test_command,
            )
            await self._send_direct_result(
                message=message,
                assistant_text=output,
                skip_user_message=False,
            )
            return True

        return False

    async def _try_handle_conduit_intent(self, message: IncomingMessage) -> bool:
        if not self._looks_like_phone_call_request(message.text or ""):
            return False
        await self._run_checkpoint_agent_command(
            message=message,
            task=message.text.strip(),
            agent_name="conduit",
            tool_use_prefix="conduit",
            unavailable_label="Conduit agent",
        )
        return True

    async def _try_handle_conduit_command(
        self, message: IncomingMessage
    ) -> IncomingMessage | bool:
        """Dispatch `/do` directly to the checkpoint-capable Conduit agent."""
        parsed = self._parse_slash_command(message.text)
        if parsed is None:
            return False
        command, arguments = parsed
        if command != "/do":
            return False
        task = arguments.strip()
        if not task:
            await self._send_direct_result(
                message=message,
                assistant_text="Usage: /do <task>",
                skip_user_message=False,
            )
            return True
        await self._run_checkpoint_agent_command(
            message=message,
            task=task,
            agent_name="conduit",
            tool_use_prefix="conduit",
            unavailable_label="Conduit agent",
        )
        return True

    async def _run_coding_agent_command(
        self,
        *,
        message: IncomingMessage,
        task: str,
        job_id: str,
        repo_path: str,
    ) -> None:
        """Invoke the coding agent directly for /code without LLM re-routing."""
        await self._run_checkpoint_agent_command(
            message=message,
            task=task,
            agent_name="coding",
            tool_use_prefix="code",
            unavailable_label="Coding agent",
            agent_input={
                "job_id": job_id,
                "repo_path": repo_path,
                "projects_root": getattr(
                    getattr(self._config, "coding", None),
                    "projects_root",
                    "/workspace/projects",
                ),
            },
            env=self._coding_env(),
        )

    async def _run_checkpoint_agent_command(
        self,
        *,
        message: IncomingMessage,
        task: str,
        agent_name: str,
        tool_use_prefix: str,
        unavailable_label: str,
        agent_input: dict[str, Any] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        """Invoke a checkpoint-capable agent and render its Telegram response."""
        if self._tool_registry is None or not self._tool_registry.has("use_agent"):
            await self._send_direct_result(
                message=message,
                assistant_text=f"{unavailable_label} is not available in this runtime.",
                skip_user_message=False,
            )
            return

        from ash.providers.telegram.checkpoint_ui import (
            create_checkpoint_keyboard,
            format_checkpoint_message,
        )
        from ash.tools.builtin.agents import CHECKPOINT_METADATA_KEY, UseAgentTool

        use_agent_tool = self._tool_registry.get("use_agent")
        if not isinstance(use_agent_tool, UseAgentTool):
            await self._send_direct_result(
                message=message,
                assistant_text=f"{unavailable_label} is not properly configured.",
                skip_user_message=False,
            )
            return

        import uuid

        thread_id = message.metadata.get("thread_id")
        session_key = make_session_key(
            self._provider.name,
            message.chat_id,
            message.user_id,
            thread_id,
        )
        session_manager = self._session_handler.get_session_manager(
            message.chat_id, message.user_id, thread_id
        )
        tracker = self._create_tool_tracker(message)
        progress_tool = ProgressMessageTool(tracker)
        tool_use_id = f"{tool_use_prefix}_{uuid.uuid4().hex[:12]}"
        tool_input = {
            "agent": agent_name,
            "message": task,
            "input": agent_input or {},
        }
        tool_context = ToolContext(
            session_id=session_key,
            user_id=message.user_id,
            chat_id=message.chat_id,
            thread_id=thread_id,
            provider=self._provider.name,
            metadata={
                "message_id": message.id,
                "current_message_id": message.id,
                "username": message.username,
                "display_name": message.display_name,
                "chat_type": message.metadata.get("chat_type"),
                "chat_title": message.metadata.get("chat_title"),
                "reply_to_message_id": message.reply_to_message_id or message.id,
            },
            session_manager=session_manager,
            tool_use_id=tool_use_id,
            tool_overrides={progress_tool.name: progress_tool},
            env=env or {},
        )

        await self._provider.send_typing(message.chat_id)
        result = await use_agent_tool.execute(tool_input, tool_context)
        response_text = _unwrap_direct_agent_output(result.content)
        reply_markup = None
        checkpoint = result.metadata.get(CHECKPOINT_METADATA_KEY)
        if isinstance(checkpoint, dict):
            self._checkpoint_handler.store_checkpoint(
                checkpoint,
                message,
                agent_name=agent_name,
                original_message=task,
                tool_use_id=tool_use_id,
            )
            response_text = format_checkpoint_message(checkpoint)
            reply_markup = create_checkpoint_keyboard(checkpoint)

        if tracker.thinking_msg_id and not reply_markup:
            sent_message_id = await tracker.finalize_response(response_text)
        else:
            if tracker.thinking_msg_id:
                try:
                    await self._provider.delete(
                        message.chat_id, tracker.thinking_msg_id
                    )
                except Exception:
                    logger.debug("Failed to delete agent thinking message")
            sent_message_id = await self._provider.send(
                OutgoingMessage(
                    chat_id=message.chat_id,
                    text=response_text,
                    reply_to_message_id=message.id,
                    reply_markup=reply_markup,
                )
            )

        await self._session_handler.persist_messages(
            message.chat_id,
            message.user_id,
            message.text,
            response_text,
            external_id=message.id,
            reply_to_external_id=message.reply_to_message_id,
            response_external_id=sent_message_id,
            username=message.username,
            display_name=message.display_name,
            thread_id=thread_id,
            skip_user_message=False,
        )
        await session_manager.add_tool_use(
            tool_use_id=tool_use_id,
            name="use_agent",
            input_data=tool_input,
        )
        await session_manager.add_tool_result(
            tool_use_id=tool_use_id,
            output=result.content,
            success=not result.is_error,
            metadata=result.metadata,
        )
        self._session_handler.mark_active_thread(
            message.chat_id, thread_id, reason="direct_agent_command"
        )
        self._log_response(response_text)

    async def _try_handle_capability_oauth_callback(
        self, message: IncomingMessage
    ) -> bool:
        """Complete capability auth from pasted callback URL without LLM orchestration."""
        callback_url = self._extract_localhost_callback_url(message.text or "")
        if not callback_url:
            return False

        manager = self._get_capability_manager()
        if manager is None:
            return False

        try:
            completion = await manager.auth_complete_callback(
                user_id=message.user_id,
                callback_url=callback_url,
                chat_id=message.chat_id,
                chat_type=message.metadata.get("chat_type"),
                provider=self._provider.name,
                thread_id=message.metadata.get("thread_id"),
                source_username=message.username,
                source_display_name=message.display_name,
            )
            pending_flows = await manager.list_auth_flows(user_id=message.user_id)
        except Exception as e:
            code = getattr(e, "code", "")
            if isinstance(code, str) and code.startswith("capability_auth_"):
                reply = (
                    "I could not apply that OAuth callback yet "
                    f"({code}). Please continue with the latest auth URL."
                )
                await self._provider.send(
                    OutgoingMessage(
                        chat_id=message.chat_id,
                        text=reply,
                        reply_to_message_id=message.id,
                    )
                )
                return True
            logger.exception(
                "capability_oauth_callback_failed",
                extra={
                    "chat_id": message.chat_id,
                    "user_id": message.user_id,
                },
            )
            reply = (
                "I hit an internal error while processing that OAuth callback. "
                "Please resend the callback URL."
            )
            await self._provider.send(
                OutgoingMessage(
                    chat_id=message.chat_id,
                    text=reply,
                    reply_to_message_id=message.id,
                )
            )
            return True

        capability = str(completion.get("capability", "")).strip()
        account_hint = str(completion.get("account_hint", "")).strip() or "default"
        capability_label = {
            "gog.email": "Gmail",
            "gog.calendar": "Google Calendar",
        }.get(capability, capability or "Google capability")

        pending_caps = sorted(
            {
                str(flow.get("capability", "")).strip()
                for flow in pending_flows
                if isinstance(flow, dict) and flow.get("capability")
            }
        )
        if pending_caps:
            pending_names = ", ".join(
                sorted(
                    {
                        {
                            "gog.email": "Gmail",
                            "gog.calendar": "Google Calendar",
                        }.get(cap, cap)
                        for cap in pending_caps
                    }
                )
            )
            reply = (
                f"{capability_label} connected for account '{account_hint}'. "
                f"Still pending: {pending_names}. Paste the next callback URL when ready."
            )
        else:
            reply = (
                f"{capability_label} connected for account '{account_hint}'. "
                "Google setup is complete."
            )

        response_external_id = await self._provider.send(
            OutgoingMessage(
                chat_id=message.chat_id,
                text=reply,
                reply_to_message_id=message.id,
            )
        )
        self._log_response(reply)
        await self._session_handler.persist_messages(
            chat_id=message.chat_id,
            user_id=message.user_id,
            user_message=message.text,
            assistant_message=reply,
            external_id=message.id,
            response_external_id=response_external_id,
            thread_id=message.metadata.get("thread_id"),
            username=message.username,
            display_name=message.display_name,
        )
        return True

    def _log_response(self, text: str | None) -> None:
        bot_name = self._provider.bot_username or "bot"
        logger.info(
            "bot_response",
            extra={
                "telegram.bot_name": bot_name,
                "output.length": len(text) if text else 0,
                "output.preview": (text or "")[:500],
            },
        )

    async def handle_message(self, message: IncomingMessage) -> None:
        """Handle an incoming Telegram message."""
        from ash.logging import log_context

        # Set chat_id context immediately so all logs have it
        # (session_id is added later in _process_single_message when known)
        with log_context(
            chat_id=message.chat_id,
            provider=self._provider.name,
            user_id=message.user_id,
            source_username=message.username,
        ):
            await self._handle_message_inner(message)

    async def _handle_message_inner(self, message: IncomingMessage) -> None:
        """Inner implementation of handle_message (runs with chat_id log context)."""
        logger.debug(
            "Received message from %s in chat %s: %s",
            message.username or message.user_id,
            message.chat_id,
            _truncate(message.text),
        )

        ctx: SessionLock | None = None
        try:
            if message.timestamp:
                age = datetime.now(UTC) - message.timestamp.replace(tzinfo=UTC)
                if age > timedelta(minutes=5):
                    logger.debug(
                        "Skipping old message %s (age=%ds)",
                        message.id,
                        age.total_seconds(),
                    )
                    return

            if await self._session_handler.should_skip_reply(message):
                logger.debug(
                    f"Skipping reply {message.id} - target not in conversation"
                )
                return

            checkpoint_thread_id = (
                await self._checkpoint_handler.resolve_text_response_thread(message)
            )
            if checkpoint_thread_id:
                message.metadata["thread_id"] = checkpoint_thread_id

            # Resolve thread from reply chain for groups (before any processing)
            thread_id = await self._session_handler.resolve_reply_chain_thread(message)
            if thread_id:
                message.metadata["thread_id"] = thread_id

            if await self._session_handler.is_duplicate_message(message):
                logger.debug(
                    "duplicate_message_skipped",
                    extra={"message.id": str(message.id)},
                )
                return

            session_key = make_session_key(
                self._provider.name, message.chat_id, message.user_id, thread_id
            )
            ctx = self._session_handler.get_session_context(session_key)

            if ctx.lock.locked():
                ctx.add_pending(message)
                await self._provider.set_reaction(message.chat_id, message.id, "👀")
                logger.info(
                    "message_queued_for_steering",
                    extra={
                        "username": message.username or message.user_id,
                        "session_id": session_key,
                    },
                )
                return

            await self._process_message_loop(message, ctx)

        except Exception:
            logger.exception("Error handling message")
            await self._provider.clear_reaction(message.chat_id, message.id)
            # Clear reactions on any pending messages that were queued
            if ctx is not None:
                for pending_msg in ctx.pending_messages:
                    await self._provider.clear_reaction(
                        pending_msg.chat_id, pending_msg.id
                    )
            await self._send_error(message.chat_id)

    async def _process_message_loop(
        self, initial_message: IncomingMessage, ctx: SessionLock
    ) -> None:
        """Process a message and any pending messages that arrive."""
        message: IncomingMessage | None = initial_message

        while message:
            async with self._concurrency_semaphore:
                async with ctx.lock:
                    await self._process_single_message(message, ctx)
                    pending = ctx.take_pending()
                    if pending:
                        message = pending[0]
                        for msg in pending[1:]:
                            ctx.add_pending(msg)
                        logger.debug(
                            "Processing queued message (remaining: %d)",
                            len(pending) - 1,
                        )
                    else:
                        message = None

    async def _process_single_message(
        self, message: IncomingMessage, ctx: SessionLock
    ) -> None:
        """Process a single message within the session lock."""
        from ash.logging import log_context

        thread_id = message.metadata.get("thread_id")
        session_key = make_session_key(
            self._provider.name, message.chat_id, message.user_id, thread_id
        )

        with log_context(
            chat_id=message.chat_id,
            session_id=session_key,
            provider=self._provider.name,
            user_id=message.user_id,
            thread_id=thread_id,
            chat_type=message.metadata.get("chat_type"),
            source_username=message.username,
        ):
            await self._process_single_message_inner(message, ctx)

    async def _process_single_message_inner(
        self, message: IncomingMessage, ctx: SessionLock
    ) -> None:
        """Inner implementation of _process_single_message (runs with log context)."""
        await self._provider.set_reaction(message.chat_id, message.id, "👀")
        raw_message = replace(message)
        preprocess_fn = getattr(self._agent, "run_incoming_message_preprocessors", None)
        if callable(preprocess_fn):
            preprocessed = preprocess_fn(message)
            candidate = (
                await preprocessed
                if inspect.isawaitable(preprocessed)
                else preprocessed
            )
            if isinstance(candidate, IncomingMessage):
                message = candidate

        self._session_handler.maybe_record_mutation_confirmation_from_user(message)

        if await self._checkpoint_handler.handle_text_response(message):
            return

        if await self._try_handle_capability_oauth_callback(message):
            return

        conduit_message = self._message_for_raw_slash_command(
            raw_message=raw_message,
            processed_message=message,
            commands={"/do"},
        )
        conduit_command_result = await self._try_handle_conduit_command(conduit_message)
        if conduit_command_result is True:
            return
        if isinstance(conduit_command_result, IncomingMessage):
            message = conduit_command_result

        coding_message = self._message_for_raw_slash_command(
            raw_message=raw_message,
            processed_message=message,
            commands={"/code", "/status", "/diff", "/test", "/cancel"},
        )
        coding_command_result = await self._try_handle_coding_command(coding_message)
        if coding_command_result is True:
            return
        if isinstance(coding_command_result, IncomingMessage):
            message = coding_command_result

        if await self._try_handle_conduit_intent(raw_message):
            return

        if await self._try_handle_coding_intent(raw_message):
            return

        if await self._try_handle_triggered_skill(message):
            return

        # Check if there's an active interactive subagent stack for this session
        thread_id = message.metadata.get("thread_id")
        session_key = make_session_key(
            self._provider.name, message.chat_id, message.user_id, thread_id
        )

        if self._stack_manager.has_active(session_key) and self._agent_executor:
            try:
                await self._handle_stack_message(message, session_key)
            finally:
                await self._provider.clear_reaction(message.chat_id, message.id)
            return

        # Try to restore a persisted stack from state.json (survives process restart)
        if self._agent_executor and await self._try_restore_stack(message, session_key):
            try:
                await self._handle_stack_message(message, session_key)
            finally:
                await self._provider.clear_reaction(message.chat_id, message.id)
            return

        session = await self._session_handler.get_or_create_session(message)

        if session.has_incomplete_tool_use():
            logger.warning(
                "session_incomplete_tool_use",
                extra={"session_id": session.session_id},
            )
            session.repair_incomplete_tool_use()

        logger.info(
            "user_message_received",
            extra={
                "username": message.username or message.user_id,
                "input.preview": _truncate(message.text),
            },
        )

        # Create tracker before try/except so it's accessible in ChildActivated handler
        tracker = self._create_tool_tracker(message)

        try:
            if self._streaming:
                await self._streaming_handler.handle_streaming(
                    message, session, ctx, tracker=tracker
                )
            else:
                await self._sync_handler.handle_sync(
                    message, session, ctx, tracker=tracker
                )
        except ChildActivated as ca:
            # A tool spawned an interactive child subagent (streaming or sync).
            # ChildActivated carries both main_frame and child_frame.
            if self._agent_executor and ca.main_frame and ca.child_frame:
                stack = self._stack_manager.get_or_create(session_key)
                stack.push(ca.main_frame)
                stack.push(ca.child_frame)
                logger.info(
                    "child_activated",
                    extra={
                        "stack_depth": stack.depth,
                        "child_agent": ca.child_frame.agent_name,
                    },
                )
                # Persist stack after initial push
                thread_id_for_sm = message.metadata.get("thread_id")
                sm = self._session_handler.get_session_manager(
                    message.chat_id, message.user_id, thread_id_for_sm
                )
                self._persist_stack(session_key, sm)
                response_external_id = await self._run_orchestration_loop(
                    message,
                    session_key,
                    entry_user_message=None,
                    thinking_msg_id=tracker.thinking_msg_id,
                    tracker=tracker,
                )
                # Persist messages so thread_index registers the bot response
                # (user message already early-persisted by sync/streaming handler)
                thread_id = message.metadata.get("thread_id")
                await self._session_handler.persist_messages(
                    message.chat_id,
                    message.user_id,
                    message.text,
                    assistant_message=None,
                    external_id=message.id,
                    response_external_id=response_external_id,
                    thread_id=thread_id,
                    username=message.username,
                    display_name=message.display_name,
                    skip_user_message=True,
                )
                # Clean up orphaned thinking message if no response sent
                if not response_external_id and tracker.thinking_msg_id:
                    try:
                        await self._provider.delete(
                            message.chat_id, tracker.thinking_msg_id
                        )
                    except Exception:
                        logger.debug("Failed to delete orphaned thinking message")
            else:
                logger.warning("child_activated_no_executor")
        finally:
            await self._provider.clear_reaction(message.chat_id, message.id)
            steered = ctx.take_steered()
            # Persist steered messages with was_steering flag
            if steered:
                thread_id = message.metadata.get("thread_id")
                await self._session_handler.persist_steered_messages(steered, thread_id)
            for msg in steered:
                await self._provider.clear_reaction(msg.chat_id, msg.id)

    def _persist_stack(self, session_key: str, sm: Any) -> None:
        """Persist the current agent stack to state.json."""
        stack = self._stack_manager._stacks.get(session_key)
        if stack and not stack.is_empty:
            metas = [f.to_meta() for f in stack.frames]
            sm.save_active_stack(metas)
        else:
            sm.save_active_stack(None)

    async def _try_restore_stack(
        self, message: IncomingMessage, session_key: str
    ) -> bool:
        """Try to restore a persisted agent stack from state.json.

        Returns True if the stack was successfully restored and loaded
        into the AgentStackManager, False otherwise.
        """
        thread_id = message.metadata.get("thread_id")
        sm = self._session_handler.get_session_manager(
            message.chat_id, message.user_id, thread_id
        )
        persisted = sm.load_active_stack()
        if not persisted:
            return False

        logger.info(
            "stack_restore_attempt",
            extra={"stack_depth": len(persisted)},
        )

        # Stale detection: if the top frame's agent_session_id has a matching
        # AgentSessionCompleteEntry, the stack finished before restart — discard it
        top_meta = persisted[-1]
        if top_meta.agent_session_id:
            from ash.sessions.types import AgentSessionCompleteEntry

            entries = await sm._reader.load_entries()
            for entry in entries:
                if (
                    isinstance(entry, AgentSessionCompleteEntry)
                    and entry.agent_session_id == top_meta.agent_session_id
                ):
                    logger.info(
                        "stack_restore_stale",
                        extra={
                            "agent_session_id": top_meta.agent_session_id,
                        },
                    )
                    sm.save_active_stack(None)
                    return False

        frames = await self._reconstruct_frames(persisted, message, sm)
        if frames is None:
            logger.warning("stack_restore_failed")
            sm.save_active_stack(None)
            return False

        # Load into AgentStackManager
        stack = self._stack_manager.get_or_create(session_key)
        for frame in frames:
            stack.push(frame)

        logger.info(
            "stack_restored",
            extra={"stack_depth": stack.depth},
        )
        return True

    async def _reconstruct_frames(
        self,
        persisted: list[Any],
        message: IncomingMessage,
        sm: Any,
    ) -> list[Any] | None:
        """Reconstruct full StackFrame objects from persisted StackFrameMeta list."""
        from ash.agents.types import AgentContext, StackFrame
        from ash.core.session import SessionState

        frames: list[StackFrame] = []

        for meta in persisted:
            # Build AgentContext from message metadata
            agent_context = AgentContext(
                session_id=sm.session_key,
                user_id=message.user_id,
                chat_id=message.chat_id,
                thread_id=message.metadata.get("thread_id"),
                provider=self._provider.name,
                voice=meta.voice,
            )

            # Rebuild session
            if meta.agent_type == "main":
                # Main frame: load from context.jsonl via get_or_create_session
                session = await self._session_handler.get_or_create_session(message)
            else:
                # Child frame: load from subagent JSONL
                if not meta.agent_session_id:
                    logger.warning(
                        "stack_restore_no_agent_session_id",
                        extra={"agent_name": meta.agent_name},
                    )
                    return None

                subagent_entries = await sm._reader.load_subagent_entries(
                    meta.agent_session_id
                )
                messages_list, _, _ = sm._reader.build_subagent_messages(
                    subagent_entries
                )

                session = SessionState(
                    session_id=f"agent-{meta.agent_name}-{sm.session_key}",
                    provider=self._provider.name,
                    chat_id=message.chat_id,
                    user_id=message.user_id,
                )
                session.messages.extend(messages_list)

            # Rebuild system_prompt
            system_prompt = self._rebuild_system_prompt(meta, agent_context)
            if system_prompt is None:
                logger.warning(
                    "stack_restore_prompt_failed",
                    extra={"agent_name": meta.agent_name},
                )
                return None

            frame = StackFrame(
                frame_id=meta.frame_id,
                agent_name=meta.agent_name,
                agent_type=meta.agent_type,
                session=session,
                system_prompt=system_prompt,
                context=agent_context,
                model_alias=getattr(meta, "model_alias", None),
                model=meta.model,
                environment=meta.environment or None,
                iteration=meta.iteration,
                max_iterations=meta.max_iterations,
                effective_tools=list(meta.effective_tools),
                is_skill_agent=meta.is_skill_agent,
                voice=meta.voice,
                parent_tool_use_id=meta.parent_tool_use_id,
                agent_session_id=meta.agent_session_id,
            )
            frames.append(frame)

        return frames

    def _rebuild_system_prompt(self, meta: Any, context: Any) -> str | None:
        """Rebuild the system prompt for a stack frame from registry lookup."""
        if meta.agent_type == "main":
            # Use a simplified main agent prompt (no memory/people context)
            return self._agent._build_system_prompt()

        agent_name = meta.agent_name

        # Try skill registry first (skill names start with "skill:")
        if agent_name.startswith("skill:") and self._skill_registry:
            skill_name = agent_name[len("skill:") :]
            if self._skill_registry.has(skill_name):
                from ash.tools.builtin.skills import SkillAgent

                skill = self._skill_registry.get(skill_name)
                agent = SkillAgent(skill)
                return agent.build_system_prompt(context)

        # Try agent registry
        if self._agent_registry and agent_name in self._agent_registry:
            agent = self._agent_registry.get(agent_name)
            return agent.build_system_prompt(context)

        logger.warning(
            "stack_restore_agent_not_found",
            extra={"agent_name": agent_name},
        )
        return None

    async def _handle_stack_message(
        self, message: IncomingMessage, session_key: str
    ) -> None:
        """Route a user message to the top of the interactive subagent stack."""
        logger.info(
            "user_message_received_stack",
            extra={
                "username": message.username or message.user_id,
                "input.preview": _truncate(message.text),
            },
        )
        tracker = self._create_tool_tracker(message)
        response_external_id = await self._run_orchestration_loop(
            message,
            session_key,
            entry_user_message=message.text,
            tracker=tracker,
        )
        # Register bot response in thread_index so follow-up replies get routed
        if response_external_id:
            thread_id = message.metadata.get("thread_id")
            if thread_id:
                thread_index = self._session_handler.get_thread_index(message.chat_id)
                thread_index.register_message(response_external_id, thread_id)

    async def _run_orchestration_loop(
        self,
        message: IncomingMessage,
        session_key: str,
        entry_user_message: str | None = None,
        thinking_msg_id: str | None = None,
        tracker: ToolTracker | None = None,
    ) -> str | None:
        """Run the interactive subagent orchestration loop.

        Processes TurnResults until we need user input (SEND_TEXT)
        or the stack is fully unwound.

        Returns the message ID of the last bot response sent, or None.
        """
        from ash.agents.types import TurnAction

        assert self._agent_executor is not None
        stack = self._stack_manager.get_or_create(session_key)

        # Get session manager for logging subagent activity
        thread_id = message.metadata.get("thread_id")
        sm = self._session_handler.get_session_manager(
            message.chat_id, message.user_id, thread_id
        )

        entry_tool_result: tuple[str, str, bool] | None = None
        response_external_id: str | None = None
        orchestration_tracker = tracker
        if orchestration_tracker is None:
            orchestration_tracker = self._create_tool_tracker(message)
        if thinking_msg_id:
            orchestration_tracker.thinking_msg_id = thinking_msg_id
        progress_tool = ProgressMessageTool(orchestration_tracker)

        while True:
            top = stack.top
            if top is None:
                # Stack is empty — shouldn't normally happen here
                logger.warning("orchestration_loop_stack_empty")
                return response_external_id

            result = await self._agent_executor.execute_turn(
                top,
                user_message=entry_user_message,
                tool_result=entry_tool_result,
                session_manager=sm,
                tool_overrides={progress_tool.name: progress_tool},
                on_tool_start=orchestration_tracker.on_tool_start,
                on_tool_complete=orchestration_tracker.on_tool_complete,
            )
            entry_user_message = None
            entry_tool_result = None

            match result.action:
                case TurnAction.SEND_TEXT:
                    # Spec reference: specs/telegram.md (Response Provenance)
                    provenance_clause: str | None = None
                    if top.agent_type == "main":
                        tool_calls = _extract_tool_calls_from_session(top.session)
                        provenance_clause = build_provenance_clause_from_tool_calls(
                            tool_calls
                        )
                    response_external_id = await self._send_stack_response(
                        message,
                        result.text,
                        thinking_msg_id=orchestration_tracker.thinking_msg_id,
                        provenance_clause=provenance_clause,
                    )
                    orchestration_tracker.thinking_msg_id = None
                    # If top is the main agent, pop it (main agent done)
                    if top.agent_type == "main":
                        main_frame = stack.pop()
                        await self._agent.run_message_postprocess_hooks(
                            user_message="",  # no user text for this completion path
                            session=main_frame.session,
                            effective_user_id=main_frame.context.user_id or "",
                        )
                        self._stack_manager.clear(session_key)
                        self._persist_stack(session_key, sm)
                    else:
                        self._persist_stack(session_key, sm)
                    return response_external_id  # Wait for next user message

                case TurnAction.COMPLETE:
                    completed = stack.pop()
                    logger.info(
                        "child_completed",
                        extra={
                            "child_agent": completed.agent_name,
                            "remaining_depth": stack.depth,
                        },
                    )
                    if stack.is_empty:
                        # Edge case: no parent to cascade to
                        if result.text and is_no_reply(result.text):
                            logger.info(
                                "child_no_reply_suppressed",
                                extra={
                                    "child_agent": completed.agent_name,
                                    "remaining_depth": stack.depth,
                                },
                            )
                            if orchestration_tracker.thinking_msg_id:
                                try:
                                    await self._provider.delete(
                                        message.chat_id,
                                        orchestration_tracker.thinking_msg_id,
                                    )
                                except Exception:
                                    logger.debug(
                                        "Failed to delete orphaned thinking message"
                                    )
                            orchestration_tracker.thinking_msg_id = None
                        elif result.text:
                            response_external_id = await self._send_stack_response(
                                message,
                                result.text,
                                thinking_msg_id=orchestration_tracker.thinking_msg_id,
                            )
                            orchestration_tracker.thinking_msg_id = None
                        self._stack_manager.clear(session_key)
                        self._persist_stack(session_key, sm)
                        return response_external_id
                    parent = stack.top
                    if (
                        completed.agent_type == "skill"
                        and parent is not None
                        and parent.agent_type == "main"
                        and is_no_reply(result.text)
                    ):
                        logger.info(
                            "child_no_reply_suppressed",
                            extra={
                                "child_agent": completed.agent_name,
                                "remaining_depth": stack.depth,
                            },
                        )
                        main_frame = stack.pop()
                        await self._agent.run_message_postprocess_hooks(
                            user_message="",
                            session=main_frame.session,
                            effective_user_id=main_frame.context.user_id or "",
                        )
                        self._stack_manager.clear(session_key)
                        self._persist_stack(session_key, sm)
                        return response_external_id
                    self._persist_stack(session_key, sm)
                    # Inject result into parent's pending tool_use
                    assert completed.parent_tool_use_id is not None
                    entry_tool_result = (
                        completed.parent_tool_use_id,
                        result.text,
                        False,
                    )
                    continue  # Resume parent

                case TurnAction.CHILD_ACTIVATED:
                    assert result.child_frame is not None
                    stack.push(result.child_frame)
                    logger.info(
                        "nested_child_activated",
                        extra={
                            "child_agent": result.child_frame.agent_name,
                            "stack_depth": stack.depth,
                        },
                    )
                    self._persist_stack(session_key, sm)
                    continue  # Run child's first turn

                case TurnAction.INTERRUPT:
                    # For now, treat interrupts in stack mode as text to user
                    response_external_id = await self._send_stack_response(
                        message,
                        result.text,
                        thinking_msg_id=orchestration_tracker.thinking_msg_id,
                    )
                    orchestration_tracker.thinking_msg_id = None
                    return response_external_id

                case TurnAction.MAX_ITERATIONS:
                    failed = stack.pop()
                    logger.warning(
                        "stack_frame_max_iterations",
                        extra={"agent_name": failed.agent_name},
                    )
                    if stack.is_empty:
                        response_external_id = await self._send_stack_response(
                            message,
                            "Agent reached maximum steps.",
                            thinking_msg_id=orchestration_tracker.thinking_msg_id,
                        )
                        orchestration_tracker.thinking_msg_id = None
                        self._stack_manager.clear(session_key)
                        self._persist_stack(session_key, sm)
                        return response_external_id
                    self._persist_stack(session_key, sm)
                    assert failed.parent_tool_use_id is not None
                    entry_tool_result = (
                        failed.parent_tool_use_id,
                        "Agent reached maximum iterations.",
                        True,  # is_error
                    )
                    continue  # Cascade error to parent

                case TurnAction.ERROR:
                    failed = stack.pop()
                    logger.error(
                        "stack_frame_error",
                        extra={
                            "agent_name": failed.agent_name,
                            "error.message": result.text,
                        },
                    )
                    if stack.is_empty:
                        response_external_id = await self._send_stack_response(
                            message,
                            result.text or "An error occurred.",
                            thinking_msg_id=orchestration_tracker.thinking_msg_id,
                        )
                        orchestration_tracker.thinking_msg_id = None
                        self._stack_manager.clear(session_key)
                        self._persist_stack(session_key, sm)
                        return response_external_id
                    self._persist_stack(session_key, sm)
                    assert failed.parent_tool_use_id is not None
                    entry_tool_result = (
                        failed.parent_tool_use_id,
                        result.text or "Agent execution error.",
                        True,
                    )
                    continue

    async def _send_stack_response(
        self,
        message: IncomingMessage,
        text: str,
        *,
        thinking_msg_id: str | None = None,
        provenance_clause: str | None = None,
    ) -> str | None:
        """Send a response from the interactive subagent stack.

        If thinking_msg_id is provided and the content fits, edits the
        thinking message instead of sending a new one. Returns the message ID.
        """
        final_text = append_inline_attribution(text, provenance_clause)

        if not final_text.strip():
            return None
        bot_name = self._provider.bot_username or "bot"
        logger.info(
            "bot_response_sent",
            extra={
                "telegram.bot_name": bot_name,
                "output.preview": _truncate(final_text),
            },
        )

        from ash.providers.telegram.provider import MAX_SEND_LENGTH

        if thinking_msg_id and len(final_text) <= MAX_SEND_LENGTH:
            await self._provider.edit(message.chat_id, thinking_msg_id, final_text)
            return thinking_msg_id

        if thinking_msg_id:
            # Content too long for edit — delete thinking message, send chunked
            try:
                await self._provider.delete(message.chat_id, thinking_msg_id)
            except Exception:
                logger.debug("Failed to delete thinking message before chunked send")

        return await self._provider.send(
            OutgoingMessage(
                chat_id=message.chat_id,
                text=final_text,
                reply_to_message_id=message.id,
            )
        )

    def _store_checkpoint(
        self,
        checkpoint: dict[str, Any],
        message: IncomingMessage,
        *,
        agent_name: str | None = None,
        original_message: str | None = None,
        tool_use_id: str | None = None,
    ) -> str:
        """Store checkpoint routing info for callback lookup and return its truncated ID."""
        return self._checkpoint_handler.store_checkpoint(
            checkpoint,
            message,
            agent_name=agent_name,
            original_message=original_message,
            tool_use_id=tool_use_id,
        )

    async def _send_error(self, chat_id: str) -> None:
        await self._provider.send(
            OutgoingMessage(
                chat_id=chat_id,
                text="Sorry, I encountered an error processing your message. Please try again.",
            )
        )

    async def handle_callback_query(self, callback_query: CallbackQuery) -> None:
        """Handle callback queries from checkpoint inline keyboards."""
        data = callback_query.data or ""
        if data.startswith("fb:"):
            await self._forward_external_callback(callback_query)
            return
        await self._checkpoint_handler.handle_callback_query(callback_query)

    async def _forward_external_callback(self, callback_query: CallbackQuery) -> None:
        import os

        import httpx

        url = os.environ.get(
            "ASH_EXTERNAL_CALLBACK_URL",
            "http://127.0.0.1:8787/webhooks/telegram/callback",
        )
        message = callback_query.message
        msg_payload: dict[str, Any] = {}
        if message is not None:
            msg_payload = {
                "message_id": message.message_id,
                "chat": {"id": message.chat.id} if message.chat else {},
                "text": getattr(message, "text", None)
                or getattr(message, "caption", None),
            }
        from_user = callback_query.from_user
        update = {
            "callback_query": {
                "id": callback_query.id,
                "from": {"id": from_user.id} if from_user else {},
                "data": callback_query.data,
                "message": msg_payload,
            }
        }
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                await client.post(url, json=update)
        except Exception as exc:
            logger.warning(
                "external_callback_forward_failed",
                extra={"error.message": str(exc), "callback.data": callback_query.data},
            )
            try:
                await callback_query.answer("Forwarding failed", show_alert=False)
            except Exception as exc:
                if is_stale_callback_error(exc):
                    logger.info(
                        "telegram_callback_expired",
                        extra={
                            "callback.id": getattr(callback_query, "id", None),
                            "callback.data": callback_query.data,
                        },
                    )
                else:
                    logger.debug("answer_callback_query_failed", exc_info=True)
            return
        try:
            await callback_query.answer()
        except Exception as exc:
            if is_stale_callback_error(exc):
                logger.info(
                    "telegram_callback_expired",
                    extra={
                        "callback.id": getattr(callback_query, "id", None),
                        "callback.data": callback_query.data,
                    },
                )
            else:
                logger.debug("answer_callback_query_failed", exc_info=True)

    def clear_session(self, chat_id: str) -> None:
        """Clear session data for a chat."""
        self._session_handler.clear_session(chat_id)

    def clear_all_sessions(self) -> None:
        """Clear all session data."""
        self._session_handler.clear_all_sessions()
        self._checkpoint_handler.clear_all_checkpoints()
