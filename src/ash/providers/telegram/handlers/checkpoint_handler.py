"""Checkpoint handling for Telegram inline keyboard callbacks.

This module provides:
- CheckpointHandler: Manages checkpoint storage and resume via callbacks
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ash.agents.types import CHECKPOINT_TTL_SECONDS
from ash.providers.base import IncomingMessage, OutgoingMessage
from ash.providers.telegram.provider import _truncate
from ash.sessions import SessionManager
from ash.sessions.types import PendingCheckpointRecord
from ash.sessions.types import session_key as make_session_key

from .tool_tracker import ProgressMessageTool, ToolTracker

if TYPE_CHECKING:
    from aiogram.types import CallbackQuery

    from ash.agents import AgentRegistry
    from ash.chats import ThreadIndex
    from ash.config import AshConfig
    from ash.providers.telegram.provider import TelegramProvider
    from ash.skills import SkillRegistry
    from ash.tools.registry import ToolRegistry

logger = logging.getLogger("telegram")
VAPI_APPROVE_OPTION = "Call now"
VAPI_REJECT_OPTION = "Don't call"
_APPROVE_TEXT = {
    "approve",
    "approved",
    "yes",
    "y",
    "ok",
    "okay",
    "proceed",
    "go ahead",
    "do it",
}
_CANCEL_TEXT = {"cancel", "no", "n", "stop", "nevermind", "never mind"}


def _normalize_checkpoint_text(text: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    normalized = re.sub(r"\b(?:it is|it s|its) (?:ok|okay)\b", "ok", normalized)
    normalized = re.sub(r"\bokay\b", "ok", normalized)
    return " ".join(normalized.split())


def _select_checkpoint_option(text: str, options: list[str]) -> str | None:
    normalized = _normalize_checkpoint_text(text)
    if not normalized:
        return None

    normalized_options = [_normalize_checkpoint_text(option) for option in options]
    if normalized in normalized_options:
        return options[normalized_options.index(normalized)]

    if len(normalized) == 1 and normalized.isalpha():
        index = ord(normalized) - ord("a")
        return options[index] if 0 <= index < len(options) else None

    if normalized.isdigit():
        index = int(normalized) - 1
        return options[index] if 0 <= index < len(options) else None

    if normalized in _APPROVE_TEXT:
        return options[0] if options else None

    if normalized in _CANCEL_TEXT and options:
        cancelish = {"cancel", "no", "stop"}
        for option, normalized_option in zip(options, normalized_options, strict=False):
            if normalized_option in cancelish:
                return option
        return options[-1]

    return None


def _select_checkpoint_response(text: str, options: Any) -> str | None:
    """Return free text for open prompts, or a validated explicit option."""
    response = text.strip()
    if not response:
        return None
    if not isinstance(options, list) or not options:
        return response
    return _select_checkpoint_option(response, [str(option) for option in options])


class CheckpointHandler:
    """Handles checkpoint storage and resume via inline keyboard callbacks.

    This handler manages the workflow when agents pause for user input:
    1. Store checkpoint routing info for callback lookup
    2. Retrieve checkpoints from cache or session log
    3. Process callback button clicks to resume agents
    """

    def __init__(
        self,
        provider: TelegramProvider,
        get_session_manager: Callable[[str, str, str | None], SessionManager],
        get_session_managers_dict: Callable[[], dict[str, SessionManager]],
        get_thread_index: Callable[[str], ThreadIndex],
        handle_message: Callable[[IncomingMessage], Coroutine[Any, Any, None]],
        get_turn_controller: Callable[[str], Any] | None = None,
        mark_active_thread: Callable[[str, str | None], None] | None = None,
        config: AshConfig | None = None,
        agent_registry: AgentRegistry | None = None,
        skill_registry: SkillRegistry | None = None,
        tool_registry: ToolRegistry | None = None,
    ):
        self._provider = provider
        self._get_session_manager = get_session_manager
        self._get_session_managers_dict = get_session_managers_dict
        self._get_thread_index = get_thread_index
        self._handle_message = handle_message
        self._get_turn_controller = get_turn_controller
        self._mark_active_thread = mark_active_thread
        self._config = config
        self._agent_registry = agent_registry
        self._skill_registry = skill_registry
        self._tool_registry = tool_registry
        self._pending_checkpoints: dict[str, dict[str, Any]] = {}
        self._hydrated_users: set[tuple[str, str]] = set()
        self._checkpoint_claim_lock = asyncio.Lock()

    def clear_all_checkpoints(self) -> None:
        """Clear all pending checkpoints from memory cache."""
        self._pending_checkpoints.clear()
        self._hydrated_users.clear()

    def store_checkpoint(
        self,
        checkpoint: dict[str, Any],
        message: IncomingMessage,
        *,
        agent_name: str | None = None,
        original_message: str | None = None,
        tool_use_id: str | None = None,
    ) -> str:
        """Store checkpoint routing info for callback lookup and return its truncated ID.

        Stores routing info in memory and the complete checkpoint in durable state.
        """
        from ash.providers.telegram.checkpoint_ui import MAX_CHECKPOINT_ID_LEN

        approval_request = checkpoint.get("approval_request")
        if (
            isinstance(approval_request, dict)
            and approval_request.get("action") == "vapi_call"
        ):
            checkpoint["options"] = [VAPI_APPROVE_OPTION, VAPI_REJECT_OPTION]

        truncated_id = checkpoint.get("checkpoint_id", "")[:MAX_CHECKPOINT_ID_LEN]
        thread_id = message.metadata.get("thread_id")
        session_key = make_session_key(
            self._provider.name, message.chat_id, message.user_id, thread_id
        )

        # Store routing info in memory for fast lookup.
        self._pending_checkpoints[truncated_id] = {
            "session_key": session_key,
            "chat_id": message.chat_id,
            "user_id": message.user_id,
            "thread_id": thread_id,
            "chat_type": message.metadata.get("chat_type"),
            "chat_title": message.metadata.get("chat_title"),
            "username": message.username,
            "display_name": message.display_name,
            "agent_name": agent_name,
            "original_message": original_message,
            "tool_use_id": tool_use_id,
            "checkpoint_id": checkpoint.get("checkpoint_id"),
            "conversation_envelope": message.metadata.get("conversation_envelope"),
        }
        self._hydrated_users.add((message.chat_id, message.user_id))
        session_manager = self._get_session_manager(
            message.chat_id, message.user_id, thread_id
        )
        checkpoint_options = checkpoint.get("options")
        session_manager.save_pending_checkpoint(
            PendingCheckpointRecord(
                checkpoint_id=str(checkpoint.get("checkpoint_id") or ""),
                prompt=str(checkpoint.get("prompt") or ""),
                options=(
                    [str(option) for option in checkpoint_options]
                    if isinstance(checkpoint_options, list)
                    else None
                ),
                agent_name=agent_name,
                original_message=original_message,
                tool_use_id=tool_use_id,
                envelope=message.metadata.get("conversation_envelope"),
                checkpoint=dict(checkpoint),
            )
        )
        if self._mark_active_thread:
            self._mark_active_thread(message.chat_id, thread_id)

        return truncated_id

    async def resolve_text_response_thread(
        self, message: IncomingMessage
    ) -> str | None:
        """Return the originating thread for an unambiguous checkpoint reply."""
        if message.metadata.get("thread_id"):
            return None

        await self._hydrate_pending_checkpoints(message.chat_id, message.user_id)
        text = (message.text or "").strip()
        if not text or not self._pending_checkpoints:
            return None

        for truncated_id, routing in reversed(self._pending_checkpoints.items()):
            if routing.get("chat_id") != message.chat_id:
                continue
            if routing.get("user_id") != message.user_id:
                continue

            session_manager = self._get_session_manager(
                routing["chat_id"], routing["user_id"], routing.get("thread_id")
            )
            result = await session_manager.get_pending_checkpoint_from_log(truncated_id)
            if not result:
                continue
            _, _, checkpoint = result
            if _select_checkpoint_response(text, checkpoint.get("options")) is not None:
                return routing.get("thread_id")

        return None

    async def _hydrate_pending_checkpoints(self, chat_id: str, user_id: str) -> None:
        """Rebuild checkpoint routing from session state after a restart."""

        identity = (chat_id, user_id)
        if identity in self._hydrated_users:
            return

        managers = list(self._get_session_managers_dict().values())
        known_keys = {manager.session_key for manager in managers}
        for manager in SessionManager.discover(
            provider=self._provider.name,
            chat_id=chat_id,
            user_id=user_id,
        ):
            if manager.session_key in known_keys:
                continue
            self._get_session_managers_dict()[manager.session_key] = manager
            managers.append(manager)
            known_keys.add(manager.session_key)

        now = datetime.now(UTC)
        for manager in managers:
            if manager.chat_id != chat_id or manager.user_id != user_id:
                continue
            for record in manager.list_pending_checkpoints():
                if (now - record.created_at).total_seconds() > CHECKPOINT_TTL_SECONDS:
                    manager.clear_pending_checkpoint(record.checkpoint_id)
                    continue
                if record.status != "pending":
                    continue
                truncated_id = record.checkpoint_id[:55]
                self._pending_checkpoints.setdefault(
                    truncated_id,
                    {
                        "session_key": manager.session_key,
                        "chat_id": chat_id,
                        "user_id": user_id,
                        "thread_id": manager.thread_id,
                        "agent_name": record.agent_name,
                        "original_message": record.original_message,
                        "tool_use_id": record.tool_use_id,
                        "checkpoint_id": record.checkpoint_id,
                        "conversation_envelope": record.envelope,
                    },
                )
        self._hydrated_users.add(identity)

    async def get_checkpoint(
        self,
        truncated_id: str,
        response_external_id: str | None = None,
        chat_id: str | None = None,
        user_id: str | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Get an authoritative pending checkpoint and its routing information."""

        async def load_from_manager(
            manager: SessionManager,
            routing: dict[str, Any] | None = None,
        ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
            record = manager.get_pending_checkpoint(truncated_id)
            if record is None or record.status != "pending":
                return None, None
            if (
                datetime.now(UTC) - record.created_at
            ).total_seconds() > CHECKPOINT_TTL_SECONDS:
                manager.clear_pending_checkpoint(record.checkpoint_id)
                self._pending_checkpoints.pop(truncated_id, None)
                return None, None

            checkpoint = record.checkpoint
            if not isinstance(checkpoint, dict):
                # One-time migration path for pending records written by older builds.
                result = await manager.get_pending_checkpoint_from_log(truncated_id)
                if result is None:
                    return None, None
                _, _, checkpoint = result

            resolved_routing = dict(routing or {})
            resolved_routing.update(
                {
                    "session_key": manager.session_key,
                    "chat_id": manager.chat_id,
                    "user_id": manager.user_id,
                    "thread_id": manager.thread_id,
                    "agent_name": record.agent_name,
                    "original_message": record.original_message,
                    "tool_use_id": record.tool_use_id,
                    "checkpoint_id": record.checkpoint_id,
                    "conversation_envelope": record.envelope,
                }
            )
            return resolved_routing, dict(checkpoint)

        if truncated_id in self._pending_checkpoints:
            routing = self._pending_checkpoints[truncated_id]
            session_manager = self._get_session_manager(
                routing["chat_id"], routing["user_id"], routing.get("thread_id")
            )
            found = await load_from_manager(session_manager, routing)
            if found[1] is not None:
                return found

        # Use the callback message only to locate a session; state.json remains authoritative.
        if response_external_id:
            for sm in self._get_session_managers_dict().values():
                if await sm.has_message_with_external_id(response_external_id):
                    found = await load_from_manager(sm)
                    if found[1] is not None:
                        logger.info(
                            "checkpoint_recovered_from_state",
                            extra={"checkpoint.id": truncated_id[:20]},
                        )
                        return found

        # Disk recovery: try loading session directly from chat/user context
        # This handles server restarts where _session_managers is empty
        if chat_id and user_id:
            await self._hydrate_pending_checkpoints(chat_id, user_id)
            if truncated_id in self._pending_checkpoints:
                routing = self._pending_checkpoints[truncated_id]
                session_manager = self._get_session_manager(
                    routing["chat_id"], routing["user_id"], routing.get("thread_id")
                )
                return await load_from_manager(session_manager, routing)

        return None, None

    async def _claim_checkpoint(
        self,
        routing: dict[str, Any],
        checkpoint: dict[str, Any],
        selected_option: str,
    ) -> PendingCheckpointRecord | None:
        checkpoint_id = str(checkpoint.get("checkpoint_id") or "")
        if not checkpoint_id:
            return None
        manager = self._get_session_manager(
            str(routing.get("chat_id") or ""),
            str(routing.get("user_id") or ""),
            routing.get("thread_id"),
        )
        async with self._checkpoint_claim_lock:
            return manager.claim_pending_checkpoint(
                checkpoint_id,
                selected_option,
                ttl_seconds=CHECKPOINT_TTL_SECONDS,
            )

    @staticmethod
    def _approval_grant(
        record: PendingCheckpointRecord, selected_option: str
    ) -> dict[str, Any] | None:
        checkpoint = record.checkpoint or {}
        request = checkpoint.get("approval_request")
        if (
            not isinstance(request, dict)
            or request.get("action") != "vapi_call"
            or selected_option != VAPI_APPROVE_OPTION
        ):
            return None
        return {
            "checkpoint_id": record.checkpoint_id,
            "approval_request": dict(request),
        }

    def clear_checkpoint(self, truncated_id: str) -> None:
        """Clear a checkpoint from both the routing cache and authoritative state."""
        routing = self._pending_checkpoints.pop(truncated_id, None)
        if routing:
            manager = self._get_session_manager(
                routing["chat_id"], routing["user_id"], routing.get("thread_id")
            )
            manager.clear_pending_checkpoint(
                str(routing.get("checkpoint_id") or truncated_id)
            )
            return
        for manager in self._get_session_managers_dict().values():
            record = manager.get_pending_checkpoint(truncated_id)
            if record is not None:
                manager.clear_pending_checkpoint(record.checkpoint_id)
                return

    async def handle_text_response(self, message: IncomingMessage) -> bool:
        """Resume a pending checkpoint from an unambiguous text reply."""
        await self._hydrate_pending_checkpoints(message.chat_id, message.user_id)
        text = (message.text or "").strip()
        if not text or not self._pending_checkpoints:
            return False

        current_thread_id = message.metadata.get("thread_id")
        for truncated_id, routing in reversed(self._pending_checkpoints.items()):
            if routing.get("chat_id") != message.chat_id:
                continue
            if routing.get("user_id") != message.user_id:
                continue
            if routing.get("thread_id") != current_thread_id:
                continue

            authoritative_routing, checkpoint = await self.get_checkpoint(
                truncated_id,
                chat_id=message.chat_id,
                user_id=message.user_id,
            )
            if authoritative_routing is None or checkpoint is None:
                continue
            selected_option = _select_checkpoint_response(
                text, checkpoint.get("options")
            )
            if selected_option is None:
                continue

            await self._resume_checkpoint_from_text(
                message=message,
                routing=authoritative_routing,
                checkpoint=checkpoint,
                selected_option=selected_option,
                truncated_id=truncated_id,
            )
            return True

        return False

    async def _resume_checkpoint_from_text(
        self,
        *,
        message: IncomingMessage,
        routing: dict[str, Any],
        checkpoint: dict[str, Any],
        selected_option: str,
        truncated_id: str,
    ) -> None:
        from ash.agents.types import CheckpointState
        from ash.tools.base import ToolContext
        from ash.tools.builtin.agents import CHECKPOINT_METADATA_KEY, UseAgentTool

        from .checkpoint_callback import ResponseFinalizer

        chat_id = routing.get("chat_id", "")
        user_id = routing.get("user_id", "")
        thread_id = routing.get("thread_id")
        session_key = routing.get("session_key", "")
        agent_name = routing.get("agent_name")
        original_message = routing.get("original_message")
        conversation_envelope = routing.get("conversation_envelope")
        checkpoint_id = checkpoint.get("checkpoint_id")

        claimed = await self._claim_checkpoint(routing, checkpoint, selected_option)
        if claimed is None:
            await self._provider.send(
                OutgoingMessage(
                    chat_id=chat_id,
                    text="That approval was already used, is being processed, or expired.",
                    reply_to_message_id=message.id,
                )
            )
            return
        approval_grant = self._approval_grant(claimed, selected_option)

        has_agent_context = agent_name and original_message and checkpoint_id
        has_tool_registry = self._tool_registry and self._tool_registry.has("use_agent")
        if not has_agent_context or not has_tool_registry:
            logger.info(
                "checkpoint_text_response_via_message_flow",
                extra={"checkpoint.id": truncated_id[:20]},
            )
            self.clear_checkpoint(truncated_id)
            metadata = {
                **message.metadata,
                "is_checkpoint_response": True,
                "checkpoint.id": checkpoint_id,
            }
            if thread_id:
                metadata["thread_id"] = thread_id
            synthetic_message = replace(
                message,
                text=selected_option,
                metadata=metadata,
            )
            await self._handle_message(synthetic_message)
            return

        assert checkpoint_id is not None
        assert self._tool_registry is not None
        use_agent_tool = self._tool_registry.get("use_agent")
        if not isinstance(use_agent_tool, UseAgentTool):
            self._get_session_manager(
                chat_id, user_id, thread_id
            ).release_pending_checkpoint(claimed.checkpoint_id)
            await self._provider.send(
                OutgoingMessage(
                    chat_id=chat_id,
                    text="Error: use_agent tool is not properly configured.",
                    reply_to_message_id=message.id,
                )
            )
            return

        existing = await use_agent_tool.get_checkpoint(checkpoint_id)
        if existing is None:
            await use_agent_tool.store_checkpoint(CheckpointState.from_dict(checkpoint))

        await self._provider.send_typing(chat_id)
        tracker = ToolTracker(
            provider=self._provider,
            chat_id=chat_id,
            reply_to=message.id,
            config=self._config,
            agent_registry=self._agent_registry,
            skill_registry=self._skill_registry,
        )
        progress_tool = ProgressMessageTool(tracker)
        tool_use_id = f"text_checkpoint_{uuid.uuid4().hex[:12]}"
        tool_input = {
            "agent": agent_name,
            "message": original_message,
            "resume_checkpoint_id": checkpoint_id,
            "checkpoint_response": selected_option,
        }
        turn_controller = (
            self._get_turn_controller(session_key)
            if self._get_turn_controller
            else None
        )
        tool_context = ToolContext(
            session_id=session_key,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            provider=self._provider.name,
            metadata={
                "current_message_id": message.id,
                **(
                    {"approval_grant": approval_grant}
                    if approval_grant is not None
                    else {}
                ),
                **(
                    {"conversation_envelope": conversation_envelope}
                    if isinstance(conversation_envelope, dict)
                    else {}
                ),
            },
            session_manager=self._get_session_manager(chat_id, user_id, thread_id),
            tool_overrides={progress_tool.name: progress_tool},
            turn_controller=turn_controller,
            cancellation_event=(
                turn_controller.cancel_event if turn_controller else None
            ),
        )

        try:
            result = await use_agent_tool.execute(tool_input, tool_context)
        except Exception:
            session_manager = self._get_session_manager(chat_id, user_id, thread_id)
            session_manager.release_pending_checkpoint(claimed.checkpoint_id)
            raise
        self.clear_checkpoint(truncated_id)

        session_manager = self._get_session_manager(chat_id, user_id, thread_id)
        thread_index = self._get_thread_index(chat_id) if thread_id else None
        finalizer = ResponseFinalizer(
            provider=self._provider,
            session_manager=session_manager,
            thread_index=thread_index,
            chat_id=chat_id,
            thread_id=thread_id,
            routing=routing,
        )
        await finalizer.finalize(
            result=result,
            tracker=tracker,
            checkpoint_message_id=message.id,
            checkpoint_id=checkpoint_id,
            selected_option=selected_option,
            user_id=user_id,
            tool_use_id=tool_use_id,
            tool_input=tool_input,
            agent_name=agent_name,
            original_message=original_message,
            store_checkpoint_fn=self.store_checkpoint,
        )
        if CHECKPOINT_METADATA_KEY not in result.metadata:
            session_manager.set_active_goal(None)

    async def handle_callback_query(self, callback_query: CallbackQuery) -> None:
        """Handle callback queries from checkpoint inline keyboards.

        When a user clicks a button on a checkpoint keyboard, this method:
        1. Parses the callback data to get checkpoint info
        2. Retrieves the stored checkpoint with agent context
        3. Calls the use_agent tool directly with resume parameters
        4. Formats and sends the result to the user
        5. Handles nested checkpoints if the resumed agent pauses again
        """
        from .checkpoint_callback import CallbackValidator

        # Parse callback data
        context, error = CallbackValidator.parse_callback_data(callback_query)
        if context is None:
            await callback_query.answer(error or "Invalid callback data")
            return

        # Retrieve checkpoint
        routing, checkpoint = await self.get_checkpoint(
            context.truncated_id,
            context.response_external_id,
            chat_id=context.chat_id,
            user_id=context.user_id,
        )
        if checkpoint is None or routing is None:
            logger.warning(
                "checkpoint_not_found", extra={"checkpoint.id": context.truncated_id}
            )
            await callback_query.answer(
                "Checkpoint not found. It may have expired or the session was lost.",
                show_alert=True,
            )
            return

        # Validate option selection
        options = checkpoint.get("options") or ["Proceed", "Cancel"]
        options_result = CallbackValidator.validate_options(
            context.option_index, options
        )
        if not options_result.success:
            await callback_query.answer(
                options_result.error_message or "Invalid option"
            )
            return

        selected_option = options[context.option_index]

        # Validate user authorization
        user_id = routing.get("user_id", "")
        user_result = CallbackValidator.validate_user(callback_query, user_id)
        if not user_result.success:
            await callback_query.answer(
                user_result.error_message or "Unauthorized",
                show_alert=user_result.show_alert,
            )
            return

        # Process with log context for traceability
        from ash.logging import log_context

        chat_id = routing.get("chat_id", "")
        session_key = routing.get("session_key", "")

        with log_context(
            chat_id=chat_id,
            session_id=session_key,
            provider=self._provider.name,
            user_id=user_id,
            thread_id=routing.get("thread_id"),
            source_username=callback_query.from_user.username,
        ):
            await self._handle_callback_query_inner(
                callback_query=callback_query,
                routing=routing,
                checkpoint=checkpoint,
                selected_option=selected_option,
                truncated_id=context.truncated_id,
            )

    async def _handle_callback_query_inner(
        self,
        callback_query: CallbackQuery,
        routing: dict[str, Any],
        checkpoint: dict[str, Any],
        selected_option: str,
        truncated_id: str,
    ) -> None:
        """Inner implementation of callback query handling (runs with log context)."""
        from ash.tools.base import ToolContext

        from .checkpoint_callback import ResponseFinalizer

        # Extract routing data
        chat_id = routing.get("chat_id", "")
        user_id = routing.get("user_id", "")
        thread_id = routing.get("thread_id")
        agent_name = routing.get("agent_name")
        original_message = routing.get("original_message")
        checkpoint_id = checkpoint.get("checkpoint_id")
        session_key = routing.get("session_key", "")

        claimed = await self._claim_checkpoint(routing, checkpoint, selected_option)
        if claimed is None:
            await callback_query.answer(
                "That approval was already used, is being processed, or expired.",
                show_alert=True,
            )
            return
        approval_grant = self._approval_grant(claimed, selected_option)

        await callback_query.answer(f"Selected: {selected_option}")

        # Store checkpoint message ID for reply threading and update the message
        message = callback_query.message
        checkpoint_message_id = str(message.message_id) if message else None

        if checkpoint_message_id:
            try:
                original_text = getattr(message, "text", None) or "Checkpoint"
                updated_text = f"{original_text}\n\n✓ Selected: {selected_option}"
                await self._provider.edit(chat_id, checkpoint_message_id, updated_text)
            except Exception as e:
                logger.debug("Failed to update message: %s", e)

        # Check if we can use direct tool invocation
        has_agent_context = agent_name and original_message and checkpoint_id
        has_tool_registry = self._tool_registry and self._tool_registry.has("use_agent")

        if not has_agent_context or not has_tool_registry:
            reason = "agent context" if not has_agent_context else "tool registry"
            logger.warning(
                "checkpoint_fallback_to_message_flow",
                extra={"checkpoint.missing": reason, "checkpoint.id": truncated_id},
            )
            self.clear_checkpoint(truncated_id)
            await self._handle_checkpoint_via_message(
                callback_query, routing, checkpoint, selected_option
            )
            return

        logger.info(
            "checkpoint_resuming",
            extra={
                "agent_name": agent_name,
                "checkpoint.id": truncated_id[:20],
                "selected_option": selected_option,
            },
        )

        await self._provider.send_typing(chat_id)

        assert self._tool_registry is not None  # Checked above via has_tool_registry

        # Restore CheckpointState to UseAgentTool's cache before calling execute
        from ash.agents.types import CheckpointState
        from ash.tools.builtin.agents import CHECKPOINT_METADATA_KEY, UseAgentTool

        use_agent_tool = self._tool_registry.get("use_agent")
        if not isinstance(use_agent_tool, UseAgentTool):
            logger.error("use_agent_tool_type_mismatch")
            await self._provider.send(
                OutgoingMessage(
                    chat_id=chat_id,
                    text="Error: use_agent tool is not properly configured.",
                    reply_to_message_id=checkpoint_message_id,
                )
            )
            self._get_session_manager(
                chat_id, user_id, thread_id
            ).release_pending_checkpoint(claimed.checkpoint_id)
            return

        # checkpoint_id is guaranteed to be non-None here (checked in has_agent_context above)
        assert checkpoint_id is not None
        existing = await use_agent_tool.get_checkpoint(checkpoint_id)
        if existing is None:
            checkpoint_state = CheckpointState.from_dict(checkpoint)
            await use_agent_tool.store_checkpoint(checkpoint_state)
            logger.info(
                "checkpoint_restored_to_cache",
                extra={"checkpoint.id": truncated_id},
            )

        # Create tracker for resume flow (reply to checkpoint message)
        tracker = ToolTracker(
            provider=self._provider,
            chat_id=chat_id,
            reply_to=checkpoint_message_id or "",
            config=self._config,
            agent_registry=self._agent_registry,
            skill_registry=self._skill_registry,
        )
        progress_tool = ProgressMessageTool(tracker)

        turn_controller = (
            self._get_turn_controller(session_key)
            if self._get_turn_controller
            else None
        )
        if turn_controller is not None:
            turn_controller.begin_turn()

        tool_context = ToolContext(
            session_id=session_key,
            user_id=user_id,
            chat_id=chat_id,
            thread_id=thread_id,
            provider=self._provider.name,
            metadata={
                "current_message_id": checkpoint_message_id,
                **(
                    {"approval_grant": approval_grant}
                    if approval_grant is not None
                    else {}
                ),
                **(
                    {"conversation_envelope": routing["conversation_envelope"]}
                    if isinstance(routing.get("conversation_envelope"), dict)
                    else {}
                ),
            },
            session_manager=self._get_session_manager(chat_id, user_id, thread_id),
            tool_overrides={progress_tool.name: progress_tool},
            turn_controller=turn_controller,
            cancellation_event=(
                turn_controller.cancel_event if turn_controller else None
            ),
        )

        tool_use_id = f"callback_{uuid.uuid4().hex[:12]}"
        tool_input = {
            "agent": agent_name,
            "message": original_message,
            "resume_checkpoint_id": checkpoint_id,
            "checkpoint_response": selected_option,
        }

        try:
            result = await use_agent_tool.execute(tool_input, tool_context)
        except Exception as e:
            logger.exception("Error calling use_agent tool directly")
            self._get_session_manager(
                chat_id, user_id, thread_id
            ).release_pending_checkpoint(claimed.checkpoint_id)
            if tracker.thinking_msg_id:
                try:
                    await self._provider.delete(chat_id, tracker.thinking_msg_id)
                except Exception as delete_err:
                    logger.debug("Failed to delete thinking message: %s", delete_err)
            await self._provider.send(
                OutgoingMessage(
                    chat_id=chat_id,
                    text=f"Error resuming agent: {e}. You can try clicking the button again.",
                    reply_to_message_id=checkpoint_message_id,
                )
            )
            return

        # Clear the checkpoint now that processing succeeded
        self.clear_checkpoint(truncated_id)

        # Finalize response using ResponseFinalizer
        session_manager = self._get_session_manager(chat_id, user_id, thread_id)
        thread_index = self._get_thread_index(chat_id) if thread_id else None

        finalizer = ResponseFinalizer(
            provider=self._provider,
            session_manager=session_manager,
            thread_index=thread_index,
            chat_id=chat_id,
            thread_id=thread_id,
            routing=routing,
        )

        sent_message_id = await finalizer.finalize(
            result=result,
            tracker=tracker,
            checkpoint_message_id=checkpoint_message_id,
            checkpoint_id=checkpoint_id,
            selected_option=selected_option,
            user_id=user_id,
            tool_use_id=tool_use_id,
            tool_input=tool_input,
            agent_name=agent_name,
            original_message=original_message,
            store_checkpoint_fn=self.store_checkpoint,
        )
        if CHECKPOINT_METADATA_KEY not in result.metadata:
            session_manager.set_active_goal(None)

        if sent_message_id and result.content.strip():
            self._log_response(result.content)

    async def _handle_checkpoint_via_message(
        self,
        callback_query: CallbackQuery,
        routing: dict[str, Any],
        checkpoint: dict[str, Any],
        selected_option: str,
    ) -> None:
        """Fall back to synthetic message flow for checkpoint handling.

        Used when agent context is not available for direct tool invocation.
        """
        from_user = callback_query.from_user
        username = from_user.username if from_user else routing.get("username")
        display_name = from_user.full_name if from_user else routing.get("display_name")

        metadata: dict[str, Any] = {
            "is_checkpoint_response": True,
            "checkpoint.id": checkpoint.get("checkpoint_id"),
        }
        for key in ("thread_id", "chat_type", "chat_title"):
            if value := routing.get(key):
                metadata[key] = value

        synthetic_message = IncomingMessage(
            id=f"callback_{callback_query.id}",
            chat_id=routing.get("chat_id", ""),
            user_id=routing.get("user_id", ""),
            text=selected_option,
            username=username,
            display_name=display_name,
            metadata=metadata,
        )

        logger.info(
            "checkpoint_callback_via_message_flow",
            extra={
                "selected_option": selected_option,
                "session_id": routing.get("session_key", ""),
            },
        )

        await self._handle_message(synthetic_message)

    def _log_response(self, text: str | None) -> None:
        bot_name = self._provider.bot_username or "bot"
        logger.info(
            "bot_response_sent",
            extra={
                "bot_name": bot_name,
                "output.preview": _truncate(text or "(no response)"),
            },
        )
