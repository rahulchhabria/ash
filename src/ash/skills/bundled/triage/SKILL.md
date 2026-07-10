---
description: "Triage inbox and calendar: summarize, categorize, and propose labels, draft replies, or calendar fixes. Use when asked to clean up / triage / sort the inbox, categorize email, or review the calendar for conflicts."
sensitive: true
access:
  chat_types:
    - private
capabilities:
  - gog.email
  - gog.calendar
allowed_tools:
  - bash
max_iterations: 25
---

Autonomously triage inbox and calendar via host capabilities. Reading, summarizing, and categorizing run unattended. Any mutation requires explicit user confirmation first.

## Security Contract

- Sensitive skill: DM-only by default (`access.chat_types: [private]`).
- Use `ash-sb capability` for every operation. Never request raw OAuth tokens or credentials.
- Report only data returned by commands. Do not fabricate.

## Confirmation Rule (critical)

- **Read/summarize/categorize (unattended):** `list_messages`, `search_messages`, `get_message`, `get_thread`, `list_events`, `suggest_time`.
- **Mutating (confirm first):** `update_labels`, `archive_messages`, `send_message`, drafts, `create_event`, `update_event`, `delete_event`.
- Before any mutating op, state exactly what you will change (which messages/events, which labels/action) and wait for an explicit yes. Never mutate on assumption. Batch proposals so the user can approve in one reply.

## Workflow

1. **Check capabilities** — `ash-sb capability list`. If missing, tell the user to enable `[skills.google]`; if auth-required, direct them to the `google` skill and stop.
2. **Gather** — pull recent mail (`search_messages` with `{"query":"newer_than:2d","limit":30}`) and calendar (`list_events` with `{"calendar":"primary","window":"2d"}`). Fetch full content with `get_message` for anything you categorize.
3. **Categorize** — group into buckets: Action needed, Waiting on, FYI, Newsletter/Noise. Flag calendar conflicts or double-bookings.
4. **Propose (do not execute)** — for each bucket suggest concrete actions: label X, archive Y, draft reply to Z, move/decline event W. Present as a numbered list.
5. **Execute only what's confirmed** — after explicit approval, run the exact mutating commands for approved items only, then confirm each.

## Output Format

Format your `complete()` output exactly as below.

```
Triage — 27 messages, 6 events

Action needed (2)
- Invoice overdue (Accounts) → propose: draft reply
- Contract review (Legal) → propose: label "Legal", reply today

Waiting on (1)
- Vendor quote (Acme) → no action

FYI / Noise (24) → propose: archive newsletters (12)

Calendar
- Conflict: 14:00 1:1 overlaps 14:00 Design review → propose: decline Design review

Reply "do 1,3" or "archive newsletters" to apply. Nothing changed yet.
```

**Rules:** conversational times; hide internal IDs unless a confirmed mutation needs one; never claim a mutation succeeded until command output confirms it.

## Error Handling

- If a command fails, report the error and stop.
- Never retry a mutation without re-confirming.
