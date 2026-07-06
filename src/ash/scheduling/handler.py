"""Schedule handler for processing scheduled tasks."""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from ash.agents.types import ChildActivated
from ash.core.session import SessionState
from ash.core.signals import is_no_reply
from ash.scheduling.types import ScheduleEntry

if TYPE_CHECKING:
    from ash.agents.executor import AgentExecutor
    from ash.agents.types import StackFrame
    from ash.core.agent import Agent
    from ash.scheduling.store import ScheduleStore

logger = logging.getLogger(__name__)

# Upper bound on exponential retry backoff (24h). Caps the retry horizon and
# keeps timedelta arithmetic well-defined for large attempt counts.
_MAX_RETRY_BACKOFF_SECONDS = 24 * 60 * 60

SCHEDULED_TASK_WRAPPER = """\
You are executing a scheduled task. Before running the task, evaluate whether it's still relevant given the delay.

<context>
Entry ID: {entry_id}
{schedule_line}
Scheduled by: {scheduled_by}
</context>

<timing>
Current time: {current_time}
Scheduled fire time: {fire_time}
Delay: {delay_human}
</timing>

<decision-guidance>
## Step 1: Classify the task

TIME-SENSITIVE tasks depend on being run close to their scheduled time:
- Greetings tied to time of day ("good morning", "good night")
- Reminders for specific moments ("remind me at 2pm to call")
- Event prompts ("daily standup", "weekly sync reminder")

TIME-INDEPENDENT tasks provide value regardless of when they run:
- Data fetching (weather, transit, stocks, news)
- Reports and summaries
- Backups and syncs
- General reminders without time context

## Step 2: Decide whether to execute

For TIME-SENSITIVE tasks:
- If delay > 2 hours AND the task's meaning has passed: SKIP
- If delay > 4 hours: Almost certainly SKIP unless task is clearly still useful
- Use judgment for delays between 30 min - 2 hours

For TIME-INDEPENDENT tasks:
- Always EXECUTE regardless of delay

## Step 3: What to output

If EXECUTING:
- Run the task normally
- Do NOT mention the delay unless it affects the task content

If SKIPPING:
- Respond with exactly: [NO_REPLY]
- Do NOT send a message explaining the skip

Do NOT apologize for delays or explain scheduling mechanics.
</decision-guidance>

<task>
{message}
</task>"""


def format_delay(seconds: float) -> str:
    """Format delay in human-readable form."""
    minutes = seconds / 60
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"~{int(minutes)} minutes"
    hours = minutes / 60
    if hours < 24:
        return f"~{hours:.1f} hours"
    days = hours / 24
    return f"~{days:.1f} days"


def _resolve_chat_type(entry: ScheduleEntry) -> str | None:
    """Resolve effective chat_type for scheduled executions.

    New entries should carry chat_type directly. For older Telegram entries
    without this field, infer private vs group from Telegram chat ID shape.
    """
    if entry.chat_type:
        return entry.chat_type

    if entry.provider == "telegram" and entry.chat_id:
        return "group" if entry.chat_id.startswith("-") else "private"

    return None


class MessageSender(Protocol):
    """Protocol for sending messages to a chat. Returns the sent message ID."""

    async def __call__(
        self, chat_id: str, text: str, *, reply_to: str | None = None
    ) -> str: ...


# Type for message registrar: (chat_id, message_id) -> None
# Registers a sent message in the thread index so replies are tracked
MessageRegistrar = Callable[[str, str], Awaitable[None]]
MessagePersister = Callable[[ScheduleEntry, str, str], Awaitable[None]]


class ScheduledTaskHandler:
    """Handles execution of scheduled tasks.

    Processes scheduled entries by:
    1. Creating an ephemeral session for the task
    2. Running the message through the agent
    3. Sending the response back via the appropriate provider
    """

    def __init__(
        self,
        agent: "Agent",
        senders: dict[str, MessageSender],
        registrars: dict[str, MessageRegistrar] | None = None,
        persisters: dict[str, MessagePersister] | None = None,
        timezone: str = "UTC",
        agent_executor: "AgentExecutor | None" = None,
        store: "ScheduleStore | None" = None,
    ):
        """Initialize the handler.

        Args:
            agent: Agent instance to process tasks.
            senders: Map of provider name -> send function.
            registrars: Map of provider name -> message registrar for thread tracking.
            persisters: Map of provider name -> message persistence callback.
            timezone: Fallback IANA timezone for computing fire times.
            agent_executor: Optional executor for running subagent skill loops.
            store: Optional schedule store used to enqueue retry attempts on failure.
        """
        self._agent = agent
        self._senders = senders
        self._registrars = registrars or {}
        self._persisters = persisters or {}
        self._timezone = timezone
        self._agent_executor = agent_executor
        self._store = store

    async def handle(self, entry: ScheduleEntry) -> None:
        """Process a scheduled task.

        Args:
            entry: The schedule entry to process.

        Raises:
            ValueError: If required routing context is missing.
        """
        # Validate routing context
        if not entry.provider or not entry.chat_id:
            logger.error(
                "scheduled_task_missing_routing",
                extra={
                    "schedule.entry_id": entry.id,
                    "messaging.provider": entry.provider,
                    "messaging.chat_id": entry.chat_id,
                },
            )
            raise ValueError("Missing required routing context (provider/chat_id)")

        logger.info(
            "scheduled_task_executing",
            extra={
                "schedule.entry_id": entry.id,
                "schedule.message_preview": entry.message[:50],
                "messaging.provider": entry.provider,
                "messaging.chat_id": entry.chat_id,
                "messaging.chat_title": entry.chat_title,
            },
        )

        # Compute fire time and delay
        fire_time = entry.previous_fire_time(self._timezone) or datetime.now(UTC)
        delay_seconds = (datetime.now(UTC) - fire_time).total_seconds()

        # Format times in entry's timezone
        tz = ZoneInfo(entry.timezone or self._timezone or "UTC")
        current_time_str = datetime.now(tz).strftime("%Y-%m-%d %H:%M")
        fire_time_str = fire_time.astimezone(tz).strftime("%Y-%m-%d %H:%M")

        logger.info(
            "scheduled_task_timing",
            extra={
                "schedule.entry_id": entry.id,
                "schedule.fire_time": fire_time_str,
                "schedule.current_time": current_time_str,
                "schedule.delay": format_delay(delay_seconds),
            },
        )

        # Build schedule line
        if entry.cron:
            schedule_line = f"Schedule: {entry.cron} (recurring)"
        else:
            schedule_line = f"Trigger: {fire_time_str} (one-shot)"

        # Build wrapped message with timing context
        prefixed_message = SCHEDULED_TASK_WRAPPER.format(
            entry_id=entry.id,
            schedule_line=schedule_line,
            scheduled_by=f"@{entry.username}" if entry.username else "unknown",
            current_time=current_time_str,
            fire_time=fire_time_str,
            delay_human=format_delay(delay_seconds),
            message=entry.message,
        )

        # Create ephemeral session for this task
        session = SessionState(
            session_id=f"scheduled_{uuid4().hex[:8]}",
            provider=entry.provider or "scheduled",
            chat_id=entry.chat_id or "",
            user_id=entry.user_id or "",
        )
        # Populate context so system prompt builder includes full context
        session.context.username = entry.username or ""
        session.context.is_scheduled_task = True
        session.context.chat_type = _resolve_chat_type(entry)
        if entry.chat_title:
            session.context.chat_title = entry.chat_title

        # Retry/notify only cover TASK EXECUTION failures. Delivery failures are
        # handled separately below and never retried — re-running the whole task
        # would duplicate any side effects (tool calls) that already succeeded.
        try:
            outcome = await self._execute_task(entry, prefixed_message, session)
        except Exception as e:
            logger.error(
                "scheduled_task_failed", extra={"error.message": str(e)}, exc_info=True
            )
            await self._handle_failure(entry, str(e))
            return

        # Success: clear any stale error carried on a persisted (periodic) entry so
        # a recovered task doesn't look permanently broken to introspection/UI.
        entry.last_error = None

        if not outcome or is_no_reply(outcome):
            logger.info("scheduled_task_no_response")
            return

        try:
            await self._send_response(entry, session, outcome)
        except Exception as e:
            # Task already executed; do not retry delivery (avoids duplicate side
            # effects). Surface the delivery failure in logs only.
            logger.error(
                "scheduled_response_send_failed",
                extra={"schedule.entry_id": entry.id, "error.message": str(e)},
                exc_info=True,
            )

    async def _execute_task(
        self,
        entry: ScheduleEntry,
        prefixed_message: str,
        session: SessionState,
    ) -> str | None:
        """Run the task through the agent, returning its text (or None).

        Handles the skill/subagent (`ChildActivated`) path internally so that a
        failure there propagates to the caller's uniform failure handling rather
        than escaping a sibling ``except`` block (skill-based scheduled tasks are
        the primary target of the retry/notify feature).
        """
        try:
            # ToolContext is created internally by the agent from session fields.
            # retrieval_query pins memory/people retrieval to the real task text so
            # the ~2KB scheduling wrapper never pollutes autonomous-run personalization.
            response = await self._agent.process_message(
                prefixed_message,
                session,
                user_id=entry.user_id,
                retrieval_query=entry.message,
            )
            return response.text
        except ChildActivated as ca:
            if self._agent_executor and ca.main_frame and ca.child_frame:
                return await self._run_skill_loop(
                    ca.main_frame, ca.child_frame, session
                )
            raise RuntimeError(
                "no agent executor available for scheduled skill task"
            ) from None

    async def _handle_failure(self, entry: ScheduleEntry, error: str) -> None:
        """React to a failed scheduled task: retry with backoff, else notify.

        Retries are enqueued as a fresh one-shot entry (new id) so the watcher's
        removal of the original one-shot does not cancel the retry. Periodic tasks
        are not retried here — their next cron occurrence is the natural retry — but
        they still notify on failure when configured. Preserves legacy no-retry
        behavior when ``max_retries`` is 0. See specs/schedule.md (Reliability).
        """
        entry.last_error = error

        wants_retry = not entry.is_periodic and entry.retry_count < entry.max_retries
        if wants_retry and self._store is None:
            # Policy asked for retries but no store was wired to enqueue them.
            logger.warning(
                "scheduled_retry_unavailable",
                extra={
                    "schedule.entry_id": entry.id,
                    "schedule.max_retries": entry.max_retries,
                },
            )
        if wants_retry and self._store is not None:
            attempt = entry.retry_count + 1
            # Cap backoff to keep timedelta arithmetic well-defined for large
            # attempt counts and to bound the retry horizon.
            backoff = min(
                entry.retry_backoff_seconds * (2 ** (attempt - 1)),
                _MAX_RETRY_BACKOFF_SECONDS,
            )
            retry_at = datetime.now(UTC) + timedelta(seconds=backoff)
            retry_entry = replace(
                entry,
                id=uuid4().hex[:8],
                trigger_at=retry_at,
                cron=None,
                last_run=None,
                retry_count=attempt,
                created_at=datetime.now(UTC),
                line_number=0,
            )
            self._store.add_entry(retry_entry)
            logger.info(
                "scheduled_task_retry_scheduled",
                extra={
                    "schedule.entry_id": entry.id,
                    "schedule.retry_entry_id": retry_entry.id,
                    "schedule.retry_attempt": attempt,
                    "schedule.retry_at": retry_at.isoformat(),
                    "schedule.backoff_seconds": backoff,
                },
            )
            return

        if entry.notify_on_failure:
            await self._notify_failure(entry, error)

    async def _notify_failure(self, entry: ScheduleEntry, error: str) -> None:
        """Send a failure notice to the originating chat.

        Wording differs by schedule type: a periodic task will still fire on its
        next cron occurrence, so it must not claim it "will not run again".
        """
        if entry.is_periodic:
            notice = (
                f"⚠️ Recurring scheduled task failed.\n\nTask: {entry.message}\n"
                f"Error: {error}\n\nIt will run again at its next scheduled time."
            )
        else:
            attempts = entry.retry_count + 1
            plural = "attempt" if attempts == 1 else "attempts"
            notice = (
                f"⚠️ Scheduled task failed after {attempts} {plural} and will not "
                f"run again automatically.\n\nTask: {entry.message}\nError: {error}"
            )
        try:
            await self._send_response(entry, self._failure_session(entry), notice)
        except Exception as send_error:  # pragma: no cover - defensive
            logger.error(
                "scheduled_failure_notify_failed",
                extra={
                    "schedule.entry_id": entry.id,
                    "error.message": str(send_error),
                },
            )

    @staticmethod
    def _failure_session(entry: ScheduleEntry) -> SessionState:
        """Minimal session for delivering a failure notice (no reply threading)."""
        return SessionState(
            session_id=f"scheduled_fail_{uuid4().hex[:8]}",
            provider=entry.provider or "scheduled",
            chat_id=entry.chat_id or "",
            user_id=entry.user_id or "",
        )

    async def _send_response(
        self,
        entry: ScheduleEntry,
        session: SessionState,
        text: str,
    ) -> None:
        """Send a response message back to the chat that scheduled the task."""
        if not entry.chat_id or not entry.provider:
            return

        sender = self._senders.get(entry.provider)
        if not sender:
            logger.warning("no_sender_configured", extra={"provider": entry.provider})
            return

        response_text = text
        if entry.username:
            response_text = f"@{entry.username} {response_text}"

        reply_to = session.context.reply_to_message_id
        message_id = await sender(entry.chat_id, response_text, reply_to=reply_to)
        logger.info(
            "scheduled_response_sent",
            extra={
                "messaging.provider": entry.provider,
                "messaging.chat_id": entry.chat_id,
                "response.preview": response_text[:50],
            },
        )

        registrar = self._registrars.get(entry.provider)
        if registrar:
            try:
                await registrar(entry.chat_id, message_id)
                logger.debug(
                    f"Registered scheduled message {message_id} in thread index"
                )
            except Exception as e:
                logger.error(
                    "scheduled_response_register_failed",
                    extra={
                        "messaging.provider": entry.provider,
                        "messaging.chat_id": entry.chat_id,
                        "schedule.entry_id": entry.id,
                        "error.type": type(e).__name__,
                        "error.message": str(e),
                    },
                )

        persister = self._persisters.get(entry.provider)
        if persister:
            try:
                await persister(entry, response_text, message_id)
            except Exception as e:
                logger.error(
                    "scheduled_response_persist_failed",
                    extra={
                        "messaging.provider": entry.provider,
                        "messaging.chat_id": entry.chat_id,
                        "schedule.entry_id": entry.id,
                        "error.type": type(e).__name__,
                        "error.message": str(e),
                    },
                )

    async def _run_skill_loop(
        self,
        main_frame: "StackFrame",
        child_frame: "StackFrame",
        session: SessionState,
    ) -> str | None:
        """Run a subagent skill loop to completion without user interaction."""
        from ash.agents.executor import run_to_completion

        assert self._agent_executor is not None
        result_text, _tool_calls = await run_to_completion(
            self._agent_executor, main_frame, child_frame
        )
        return result_text
