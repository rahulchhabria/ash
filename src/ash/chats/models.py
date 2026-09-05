"""Chat state models."""

from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field


class Participant(BaseModel):
    """A participant in a chat."""

    id: str
    username: str | None = None
    display_name: str | None = None
    session_id: str | None = None  # Reference to session key
    is_bot: bool = False
    first_seen: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_active: datetime = Field(default_factory=lambda: datetime.now(UTC))
    joined_at: datetime | None = None  # When they joined (if we saw the event)
    left: bool = False  # True if they left the chat
    graph_user_id: str | None = None  # Reference to graph UserEntry.id


class ChatInfo(BaseModel):
    """Chat metadata."""

    id: str
    type: str | None = None  # "private", "group", "supergroup", "channel"
    title: str | None = None


class MutationConfirmation(BaseModel):
    """Chat-scoped proof that a mutating operation was shown and confirmed."""

    plan_id: str
    capability_id: str
    operation: str
    status: str = "presented"  # presented | confirmed | executed
    presented_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime
    confirmed_at: datetime | None = None
    executed_at: datetime | None = None
    target_fingerprint: str | None = None
    thread_id: str | None = None
    summary: str | None = None


class ActiveFocus(BaseModel):
    """A recent external item the chat can naturally refer back to."""

    kind: str
    source_id: str
    title: str
    summary: str | None = None
    telegram_message_id: str | None = None
    thread_id: str | None = None
    entities: list[str] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime


class ChatState(BaseModel):
    """State for a chat, stored in state.json."""

    chat: ChatInfo
    participants: list[Participant] = Field(default_factory=list)
    thread_index: dict[str, str] = Field(default_factory=dict)
    active_thread_id: str | None = None
    active_thread_updated_at: datetime | None = None
    active_thread_reason: str | None = None
    active_focus: list[ActiveFocus] = Field(default_factory=list)
    mutation_confirmations: list[MutationConfirmation] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    graph_chat_id: str | None = None  # Reference to graph ChatEntry.id

    def get_participant(self, user_id: str) -> Participant | None:
        """Get a participant by ID."""
        return next((p for p in self.participants if p.id == user_id), None)

    def update_participant(
        self,
        user_id: str,
        username: str | None = None,
        display_name: str | None = None,
        session_id: str | None = None,
    ) -> Participant:
        """Update or add a participant, returns the participant."""
        now = datetime.now(UTC)
        participant = self.get_participant(user_id)

        if participant:
            participant.last_active = now
            if username is not None:
                participant.username = username
            if display_name is not None:
                participant.display_name = display_name
            if session_id is not None:
                participant.session_id = session_id
        else:
            participant = Participant(
                id=user_id,
                username=username,
                display_name=display_name,
                session_id=session_id,
                first_seen=now,
                last_active=now,
            )
            self.participants.append(participant)

        self.updated_at = now
        return participant

    def set_active_thread(
        self,
        thread_id: str,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> None:
        """Record the active thread for chat-scoped DM routing."""
        ts = now or datetime.now(UTC)
        self.active_thread_id = str(thread_id)
        self.active_thread_updated_at = ts
        self.active_thread_reason = reason
        self.updated_at = ts

    def get_active_thread(
        self,
        *,
        max_age_minutes: int,
        now: datetime | None = None,
    ) -> str | None:
        """Return active thread_id if it is still within the freshness window."""
        if not self.active_thread_id or not self.active_thread_updated_at:
            return None
        ts = now or datetime.now(UTC)
        max_age = max(1, int(max_age_minutes))
        if ts - self.active_thread_updated_at > timedelta(minutes=max_age):
            return None
        return self.active_thread_id

    def add_active_focus(
        self,
        *,
        kind: str,
        source_id: str,
        title: str,
        summary: str | None = None,
        telegram_message_id: str | None = None,
        thread_id: str | None = None,
        entities: list[str] | None = None,
        metadata: dict[str, str] | None = None,
        ttl_minutes: int = 240,
        max_items: int = 8,
        now: datetime | None = None,
    ) -> ActiveFocus:
        """Record a recent external item as conversational focus."""
        ts = now or datetime.now(UTC)
        self.prune_expired_focus(now=ts)
        focus = ActiveFocus(
            kind=kind,
            source_id=source_id,
            title=title.strip() or source_id,
            summary=(summary or "").strip() or None,
            telegram_message_id=(
                str(telegram_message_id) if telegram_message_id is not None else None
            ),
            thread_id=str(thread_id) if thread_id is not None else None,
            entities=_dedupe_focus_values(entities or []),
            metadata=metadata or {},
            created_at=ts,
            expires_at=ts + timedelta(minutes=max(1, int(ttl_minutes))),
        )
        self.active_focus = [
            item
            for item in self.active_focus
            if not (item.kind == focus.kind and item.source_id == focus.source_id)
        ]
        self.active_focus.append(focus)
        limit = max(1, int(max_items))
        if len(self.active_focus) > limit:
            self.active_focus = self.active_focus[-limit:]
        self.updated_at = ts
        return focus

    def get_recent_focus(
        self,
        *,
        kind: str | None = None,
        now: datetime | None = None,
    ) -> list[ActiveFocus]:
        """Return non-expired focus items, newest first."""
        ts = now or datetime.now(UTC)
        self.prune_expired_focus(now=ts)
        items = self.active_focus
        if kind is not None:
            items = [item for item in items if item.kind == kind]
        return list(reversed(items))

    def prune_expired_focus(self, *, now: datetime | None = None) -> None:
        """Remove expired conversational focus entries."""
        ts = now or datetime.now(UTC)
        before = len(self.active_focus)
        self.active_focus = [item for item in self.active_focus if item.expires_at > ts]
        if len(self.active_focus) != before:
            self.updated_at = ts

    def add_mutation_confirmation(
        self,
        *,
        plan_id: str,
        capability_id: str,
        operation: str,
        target_fingerprint: str | None = None,
        thread_id: str | None = None,
        summary: str | None = None,
        ttl_hours: int = 24,
        now: datetime | None = None,
    ) -> MutationConfirmation:
        """Store a mutation confirmation prompt shown to the user."""
        ts = now or datetime.now(UTC)
        self.prune_expired_mutation_confirmations(now=ts)
        confirmation = MutationConfirmation(
            plan_id=plan_id,
            capability_id=capability_id,
            operation=operation,
            expires_at=ts + timedelta(hours=max(1, int(ttl_hours))),
            target_fingerprint=target_fingerprint,
            thread_id=thread_id,
            summary=summary,
        )
        self.mutation_confirmations.append(confirmation)
        self.updated_at = ts
        return confirmation

    def confirm_latest_mutation(
        self,
        *,
        now: datetime | None = None,
        thread_id: str | None = None,
    ) -> MutationConfirmation | None:
        """Confirm the latest non-expired presented mutation plan."""
        ts = now or datetime.now(UTC)
        self.prune_expired_mutation_confirmations(now=ts)
        for confirmation in reversed(self.mutation_confirmations):
            if confirmation.status != "presented":
                continue
            if (
                thread_id
                and confirmation.thread_id
                and confirmation.thread_id != thread_id
            ):
                continue
            confirmation.status = "confirmed"
            confirmation.confirmed_at = ts
            self.updated_at = ts
            return confirmation
        return None

    def find_confirmed_mutation(
        self,
        *,
        capability_id: str,
        operation: str,
        target_fingerprint: str | None = None,
        thread_id: str | None = None,
        now: datetime | None = None,
    ) -> MutationConfirmation | None:
        """Find a non-expired confirmed mutation authorization."""
        ts = now or datetime.now(UTC)
        self.prune_expired_mutation_confirmations(now=ts)
        for confirmation in reversed(self.mutation_confirmations):
            if confirmation.status != "confirmed":
                continue
            if confirmation.capability_id != capability_id:
                continue
            if confirmation.operation != operation:
                continue
            if target_fingerprint and confirmation.target_fingerprint:
                if confirmation.target_fingerprint != target_fingerprint:
                    continue
            if (
                thread_id
                and confirmation.thread_id
                and confirmation.thread_id != thread_id
            ):
                continue
            return confirmation
        return None

    def mark_mutation_executed(
        self,
        *,
        plan_id: str,
        now: datetime | None = None,
    ) -> bool:
        """Mark a confirmed mutation plan as executed."""
        ts = now or datetime.now(UTC)
        for confirmation in self.mutation_confirmations:
            if confirmation.plan_id != plan_id:
                continue
            confirmation.status = "executed"
            confirmation.executed_at = ts
            self.updated_at = ts
            return True
        return False

    def prune_expired_mutation_confirmations(
        self,
        *,
        now: datetime | None = None,
    ) -> None:
        """Remove expired mutation confirmation entries."""
        ts = now or datetime.now(UTC)
        kept = [item for item in self.mutation_confirmations if item.expires_at > ts]
        if len(kept) != len(self.mutation_confirmations):
            self.mutation_confirmations = kept
            self.updated_at = ts


def _dedupe_focus_values(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = " ".join(str(value).strip().lower().split())
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result
