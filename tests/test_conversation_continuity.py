"""Regression coverage for durable conversation continuity and steering."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from ash.agents.executor import AgentExecutor
from ash.agents.types import AgentContext
from ash.config.models import ConversationConfig
from ash.core.conversation import (
    ConversationEnvelope,
    ConversationTurn,
    MemorySnippet,
    render_conversation_envelope,
)
from ash.core.session import SessionState
from ash.core.steering import TurnController
from ash.llm.types import ToolUse
from ash.memory.postprocess import MemoryPostprocessService
from ash.providers.base import IncomingMessage
from ash.providers.telegram.handlers.checkpoint_handler import CheckpointHandler
from ash.providers.telegram.handlers.session_handler import SessionHandler
from ash.sessions import SessionManager
from ash.sessions.reader import SessionReader
from ash.sessions.types import CompactionEntry, OperationState, PendingCheckpointRecord


@pytest.mark.asyncio
async def test_external_message_deduplication_does_not_rewind_parent(tmp_path) -> None:
    manager = SessionManager(
        provider="telegram",
        chat_id="chat",
        user_id="user",
        sessions_path=tmp_path,
    )

    user_id = await manager.add_user_message(
        "Call the store", metadata={"external_id": "telegram-1"}
    )
    assistant_id = await manager.add_assistant_message(
        "Ready for approval", metadata={"external_id": "telegram-2"}
    )

    duplicate_id = await manager.add_user_message(
        "Call the store", metadata={"external_id": "telegram-1"}
    )
    followup_id = await manager.add_user_message(
        "Approved", metadata={"external_id": "telegram-3"}
    )

    entries = await manager.load_message_entries_since(None)
    followup = next(entry for entry in entries if entry.id == followup_id)
    assert duplicate_id == user_id
    assert len(entries) == 3
    assert followup.parent_id == assistant_id


@pytest.mark.asyncio
async def test_new_manager_continues_from_latest_persisted_message(tmp_path) -> None:
    manager = SessionManager(
        provider="telegram",
        chat_id="chat",
        user_id="user",
        sessions_path=tmp_path,
    )
    await manager.add_user_message("first")
    latest_id = await manager.add_assistant_message("second")

    restored = SessionManager(
        provider="telegram",
        chat_id="chat",
        user_id="user",
        sessions_path=tmp_path,
    )
    next_id = await restored.add_user_message("third")
    entries = await restored.load_message_entries_since(None)
    next_entry = next(entry for entry in entries if entry.id == next_id)

    assert next_entry.parent_id == latest_id


@pytest.mark.asyncio
async def test_compaction_restores_summary_at_persisted_boundary(tmp_path) -> None:
    manager = SessionManager(
        provider="telegram",
        chat_id="chat",
        user_id="user",
        sessions_path=tmp_path,
    )
    await manager.add_user_message("old request")
    await manager.add_assistant_message("old response")
    kept_id = await manager.add_user_message("approved call details")
    await manager.add_assistant_message("placing the approved call")
    await manager.add_compaction(
        summary="The user approved a call to the store.",
        tokens_before=5000,
        tokens_after=500,
        first_kept_entry_id=kept_id,
    )

    restored = SessionManager(
        provider="telegram",
        chat_id="chat",
        user_id="user",
        sessions_path=tmp_path,
    )
    messages, message_ids = await restored.load_messages_for_llm()

    assert "user approved a call" in messages[0].get_text()
    assert [message.get_text() for message in messages[1:]] == [
        "approved call details",
        "placing the approved call",
    ]
    assert message_ids[1] == kept_id


@pytest.mark.asyncio
async def test_branch_compaction_does_not_prune_linear_context(tmp_path) -> None:
    manager = SessionManager(
        provider="telegram",
        chat_id="chat",
        user_id="user",
        sessions_path=tmp_path,
    )
    first_id = await manager.add_user_message("linear root")
    second_id = await manager.add_assistant_message("linear response")
    await manager.add_compaction(
        summary="A summary for a reply branch only.",
        tokens_before=1000,
        tokens_after=100,
        first_kept_entry_id=second_id,
        branch_id="branch-only",
    )

    messages, message_ids = await manager.load_messages_for_llm()

    assert [message.get_text() for message in messages] == [
        "linear root",
        "linear response",
    ]
    assert message_ids == [first_id, second_id]


@pytest.mark.asyncio
async def test_latest_compaction_is_scoped_to_requested_branch(tmp_path) -> None:
    manager = SessionManager(
        provider="telegram",
        chat_id="chat",
        user_id="user",
        sessions_path=tmp_path,
    )
    linear_id = await manager.add_user_message("linear context")
    branch_id = await manager.add_assistant_message("branch context")
    await manager.add_compaction(
        summary="Linear summary",
        tokens_before=1000,
        tokens_after=100,
        first_kept_entry_id=linear_id,
    )
    await manager.add_compaction(
        summary="Branch summary",
        tokens_before=1200,
        tokens_after=120,
        first_kept_entry_id=branch_id,
        branch_id="branch-1",
    )

    linear = await manager.get_latest_compaction()
    branch = await manager.get_latest_compaction("branch-1")

    assert linear is not None and linear.summary == "Linear summary"
    assert branch is not None and branch.summary == "Branch summary"


@pytest.mark.asyncio
async def test_branch_ignores_newer_compaction_from_divergent_path(tmp_path) -> None:
    manager = SessionManager(
        provider="telegram",
        chat_id="chat",
        user_id="user",
        sessions_path=tmp_path,
    )
    await manager.add_user_message("shared root")
    fork_point = await manager.add_assistant_message("shared response")
    main_tip = await manager.add_user_message("main-only request")
    branch_id = manager.fork_at_message(fork_point)
    branch_tip = await manager.add_user_message("branch-only request")
    manager.update_branch_head(branch_id, branch_tip)
    await manager.add_compaction(
        summary="Branch-safe summary",
        tokens_before=1000,
        tokens_after=100,
        first_kept_entry_id=branch_tip,
        branch_id=branch_id,
    )
    divergent_boundary = await manager.add_assistant_message(
        "main-only response", parent_id=main_tip
    )
    await manager.add_compaction(
        summary="Divergent main summary",
        tokens_before=2000,
        tokens_after=200,
        first_kept_entry_id=divergent_boundary,
    )

    branch = await manager.get_latest_compaction(branch_id)

    assert branch is not None
    assert branch.summary == "Branch-safe summary"


@pytest.mark.asyncio
async def test_duplicate_compaction_record_is_not_appended(tmp_path) -> None:
    manager = SessionManager(
        provider="telegram",
        chat_id="chat",
        user_id="user",
        sessions_path=tmp_path,
    )
    kept_id = await manager.add_user_message("kept context")
    for _ in range(2):
        await manager.add_compaction(
            summary="Stable summary",
            tokens_before=1000,
            tokens_after=100,
            first_kept_entry_id=kept_id,
        )

    entries = await SessionReader(manager.session_dir).load_entries()

    assert sum(isinstance(entry, CompactionEntry) for entry in entries) == 1


def test_legacy_compaction_without_boundary_remains_readable() -> None:
    entry = CompactionEntry.from_dict(
        {
            "type": "compaction",
            "id": "legacy-compaction",
            "summary": "Legacy summary",
            "tokens_before": 1000,
            "tokens_after": 100,
            "created_at": datetime.now(UTC).isoformat(),
        }
    )

    assert entry.first_kept_entry_id is None


def test_working_state_survives_new_manager_instance(tmp_path) -> None:
    manager = SessionManager(
        provider="telegram",
        chat_id="chat",
        user_id="user",
        thread_id="thread",
        sessions_path=tmp_path,
    )
    manager.set_active_goal("Call Adidas and ask about size 10")
    manager.save_pending_checkpoint(
        PendingCheckpointRecord(
            checkpoint_id="checkpoint-1",
            prompt="Place the call?",
            options=["Call", "Cancel"],
        )
    )
    manager.record_operation(
        OperationState(
            kind="vapi_call",
            operation_id="call-1",
            status="queued",
            idempotency_key="stable-key",
            destination="+14085550100",
            objective="Ask about size 10",
        )
    )
    manager.set_memory_extraction_cursor("message-9")

    restored = SessionManager(
        provider="telegram",
        chat_id="chat",
        user_id="user",
        thread_id="thread",
        sessions_path=tmp_path,
    ).load_conversation_state()

    assert restored.active_goal == "Call Adidas and ask about size 10"
    assert restored.pending_checkpoints[0].checkpoint_id == "checkpoint-1"
    assert restored.operations[0].operation_id == "call-1"
    assert restored.memory_extraction_cursor == "message-9"


def test_checkpoint_approval_can_be_claimed_and_consumed_once(tmp_path) -> None:
    manager = SessionManager(
        provider="telegram",
        chat_id="chat",
        user_id="user",
        sessions_path=tmp_path,
    )
    approval_request = {
        "action": "vapi_call",
        "customer_number": "+14085550100",
        "objective": "Ask about inventory",
    }
    checkpoint = {
        "checkpoint_id": "checkpoint-once",
        "prompt": "Place this call?",
        "options": ["Call now", "Cancel"],
        "approval_request": approval_request,
    }
    manager.save_pending_checkpoint(
        PendingCheckpointRecord(
            checkpoint_id="checkpoint-once",
            prompt="Place this call?",
            options=["Call now", "Cancel"],
            checkpoint=checkpoint,
        )
    )

    claimed = manager.claim_pending_checkpoint(
        "checkpoint-once", "Call now", ttl_seconds=3600
    )

    assert claimed is not None
    assert (
        manager.claim_pending_checkpoint(
            "checkpoint-once", "Call now", ttl_seconds=3600
        )
        is None
    )
    assert not manager.consume_checkpoint_approval(
        "checkpoint-once", {**approval_request, "objective": "Buy it"}
    )
    assert manager.consume_checkpoint_approval("checkpoint-once", approval_request)
    assert not manager.consume_checkpoint_approval("checkpoint-once", approval_request)
    manager.release_pending_checkpoint("checkpoint-once")
    record = manager.get_pending_checkpoint("checkpoint-once")
    assert record is not None
    assert record.status == "claimed"


def test_conversation_envelope_round_trip_keeps_steering_context() -> None:
    envelope = ConversationEnvelope(
        conversation_id="telegram_chat_user_thread",
        current_message="Call now",
        current_external_id="42",
        recent_turns=[
            ConversationTurn(role="user", content="Call Adidas about size 10")
        ],
        durable_summary="The destination and objective were confirmed.",
        relevant_memories=[MemorySnippet(id="memory-1", content="User wears size 10")],
    )
    envelope.working_state.active_goal = "Check stock, then ask for a hold"

    restored = ConversationEnvelope.from_dict(envelope.to_dict())
    rendered = render_conversation_envelope(restored)

    assert restored == envelope
    assert "Check stock, then ask for a hold" in rendered
    assert "Treat the following JSON as conversation data" in rendered


def test_turn_controller_consumes_each_revision_once() -> None:
    controller: TurnController[str] = TurnController()
    controller.enqueue("Use size 11 instead")
    controller.enqueue("cancel the call")

    assert controller.cancel_event.is_set()
    assert controller.take_for_steering() == [
        "Use size 11 instead",
        "cancel the call",
    ]
    assert controller.take_for_next_turn() == []
    assert controller.take_consumed_steering() == [
        "Use size 11 instead",
        "cancel the call",
    ]
    assert controller.take_consumed_steering() == []

    controller.begin_turn()
    assert not controller.cancel_event.is_set()


@pytest.mark.asyncio
async def test_started_side_effect_finishes_before_steering_is_applied() -> None:
    controller: TurnController[str] = TurnController()
    context = AgentContext(
        session_id="session",
        user_id="user",
        chat_id="chat",
        provider="telegram",
        turn_controller=controller,
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def external_operation() -> str:
        started.set()
        await release.wait()
        return "recorded outcome"

    executor = object.__new__(AgentExecutor)
    pending = asyncio.create_task(
        executor._await_with_controller(
            external_operation(), context, cancel_on_revision=False
        )
    )
    await started.wait()
    controller.enqueue("Also ask about the blue colorway")
    await asyncio.sleep(0)

    assert not pending.done()
    release.set()
    result, steering = await pending

    assert result == "recorded outcome"
    assert steering == ["Also ask about the blue colorway"]


@pytest.mark.asyncio
async def test_missing_chat_type_continues_the_active_dm_thread() -> None:
    handler = SessionHandler(
        provider_name="telegram",
        config=None,
        conversation_config=ConversationConfig(),
    )

    first = IncomingMessage(id="100", chat_id="chat", user_id="user", text="Call")
    first_thread = await handler.resolve_reply_chain_thread(first)
    second = IncomingMessage(id="101", chat_id="chat", user_id="user", text="Approved")
    second_thread = await handler.resolve_reply_chain_thread(second)

    assert first_thread == "100"
    assert second_thread == first_thread


@pytest.mark.asyncio
async def test_checkpoint_route_recovers_from_durable_state(tmp_path) -> None:
    manager = SessionManager(
        provider="telegram",
        chat_id="chat",
        user_id="user",
        thread_id="thread",
        sessions_path=tmp_path,
    )
    checkpoint = {
        "checkpoint_id": "checkpoint-1234567890",
        "prompt": "Place the call?",
        "options": ["Cancel", "Do it"],
        "tool_use_id": "tool-1",
        "approval_request": {
            "action": "vapi_call",
            "customer_number": "+14085550100",
            "objective": "Ask about inventory",
        },
    }
    await manager.add_assistant_message(
        [ToolUse(id="tool-1", name="interrupt", input={"prompt": "Place?"})]
    )
    await manager.add_tool_result(
        "tool-1",
        "Place the call?",
        success=True,
        metadata={"checkpoint": checkpoint},
    )

    provider = MagicMock()
    provider.name = "telegram"
    managers = {manager.session_key: manager}

    def get_manager(chat_id: str, user_id: str, thread_id: str | None):
        key = manager.session_key
        return managers.setdefault(
            key,
            SessionManager(
                provider="telegram",
                chat_id=chat_id,
                user_id=user_id,
                thread_id=thread_id,
                sessions_path=tmp_path,
            ),
        )

    initial = CheckpointHandler(
        provider=provider,
        get_session_manager=get_manager,
        get_session_managers_dict=lambda: managers,
        get_thread_index=MagicMock(),
        handle_message=AsyncMock(),
    )
    message = IncomingMessage(
        id="message-1",
        chat_id="chat",
        user_id="user",
        text="Call Adidas",
        metadata={
            "thread_id": "thread",
            "conversation_envelope": {"conversation_id": manager.session_key},
        },
    )
    initial.store_checkpoint(
        checkpoint,
        message,
        agent_name="conduit",
        original_message="Call Adidas",
        tool_use_id="tool-1",
    )

    # Simulate a process restart: a fresh handler and manager cache.
    restored_manager = SessionManager(
        provider="telegram",
        chat_id="chat",
        user_id="user",
        thread_id="thread",
        sessions_path=tmp_path,
    )
    managers = {restored_manager.session_key: restored_manager}
    restored = CheckpointHandler(
        provider=provider,
        get_session_manager=get_manager,
        get_session_managers_dict=lambda: managers,
        get_thread_index=MagicMock(),
        handle_message=AsyncMock(),
    )

    routing, recovered = await restored.get_checkpoint(
        checkpoint["checkpoint_id"][:55],
        chat_id="chat",
        user_id="user",
    )

    assert recovered == checkpoint
    assert routing is not None
    assert routing["thread_id"] == "thread"
    assert routing["original_message"] == "Call Adidas"
    assert routing["conversation_envelope"] == {"conversation_id": manager.session_key}
    assert recovered["options"] == ["Call now", "Don't call"]


@pytest.mark.asyncio
async def test_memory_cursor_only_extracts_unseen_messages(tmp_path) -> None:
    manager = SessionManager(
        provider="telegram",
        chat_id="chat",
        user_id="user",
        sessions_path=tmp_path,
    )
    await manager.add_user_message("My shoe size is 10")
    first_tail_id = await manager.add_assistant_message("Got it")
    session = SessionState(
        session_id=manager.session_key,
        provider="telegram",
        chat_id="chat",
        user_id="user",
        session_manager=manager,
    )

    class RecordingService(MemoryPostprocessService):
        def __init__(self) -> None:
            super().__init__(
                store=MagicMock(),
                extractor=MagicMock(),
                extraction_enabled=True,
                min_message_length=1,
                debounce_seconds=0,
                context_messages=10,
                confidence_threshold=0.5,
            )
            self.batches: list[list[str]] = []

        async def _extract_messages(self, **kwargs) -> None:
            self.batches.append(
                [message.get_text() for message in kwargs["thread_messages"]]
            )

    service = RecordingService()
    await service._extract_background(session=session, user_id="user", chat_id="chat")

    assert service.batches == [["My shoe size is 10", "Got it"]]
    assert manager.get_memory_extraction_cursor() == first_tail_id

    await manager.add_user_message("Actually, make that size 11")
    await service._extract_background(session=session, user_id="user", chat_id="chat")

    assert service.batches[-1] == ["Actually, make that size 11"]


@pytest.mark.asyncio
async def test_trailing_memory_debounce_is_scoped_per_conversation() -> None:
    class RecordingService(MemoryPostprocessService):
        def __init__(self) -> None:
            super().__init__(
                store=MagicMock(),
                extractor=MagicMock(),
                extraction_enabled=True,
                min_message_length=1,
                debounce_seconds=0.01,
                context_messages=10,
                confidence_threshold=0.5,
            )
            self.extracted: list[str] = []

        async def _extract_background(self, *, session, user_id, chat_id) -> None:
            self.extracted.append(session.session_id)

    service = RecordingService()
    first = SessionState("first", "telegram", "chat-a", "user")
    second = SessionState("second", "telegram", "chat-b", "user")

    service.maybe_schedule(
        user_message="Initial size is 10", session=first, effective_user_id="user"
    )
    service.maybe_schedule(
        user_message="Correction: size 11", session=first, effective_user_id="user"
    )
    service.maybe_schedule(
        user_message="A separate conversation", session=second, effective_user_id="user"
    )
    await asyncio.sleep(0.05)

    assert service.extracted.count("first") == 1
    assert service.extracted.count("second") == 1


@pytest.mark.asyncio
async def test_memory_extraction_is_not_cancelled_by_new_turn() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class RecordingService(MemoryPostprocessService):
        def __init__(self) -> None:
            super().__init__(
                store=MagicMock(),
                extractor=MagicMock(),
                extraction_enabled=True,
                min_message_length=1,
                debounce_seconds=0,
                context_messages=10,
                confidence_threshold=0.5,
            )
            self.calls = 0
            self.first_cancelled = False

        async def _extract_background(self, *, session, user_id, chat_id) -> None:
            self.calls += 1
            if self.calls != 1:
                return
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                self.first_cancelled = True
                raise

    service = RecordingService()
    session = SessionState("session", "telegram", "chat", "user")
    service.maybe_schedule(
        user_message="My size is 10", session=session, effective_user_id="user"
    )
    await started.wait()

    service.maybe_schedule(
        user_message="Actually, size 11", session=session, effective_user_id="user"
    )
    release.set()
    for _ in range(20):
        if service.calls == 2:
            break
        await asyncio.sleep(0.01)

    assert service.first_cancelled is False
    assert service.calls == 2


def test_memory_debounce_timestamp_is_scoped_per_conversation() -> None:
    service = MemoryPostprocessService(
        store=MagicMock(),
        extractor=MagicMock(),
        extraction_enabled=True,
        min_message_length=1,
        debounce_seconds=60,
        context_messages=10,
        confidence_threshold=0.5,
    )
    first_key = ("telegram", "chat-a", "user", "")
    second_key = ("telegram", "chat-b", "user", "")

    service.touch_debounce(first_key)

    assert not service._should_extract("Remember this", first_key)
    assert service._should_extract("Remember this", second_key)
