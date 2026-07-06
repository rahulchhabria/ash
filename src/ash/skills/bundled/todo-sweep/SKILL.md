---
description: "Review open and overdue todos and drive them forward — nudge, propose next steps, and escalate what's slipping. Use for a todo sweep, a 'what's overdue' check, or to set up a recurring follow-through on a schedule."
allowed_tools:
  - bash
max_iterations: 15
---

Turn the todo list from a passive ledger into an active work queue. This skill
reviews open todos, surfaces what's overdue or at risk, and drives follow-through
— it does not silently mutate the list. Orchestration lives here in the skill;
the todo subsystem stays a simple ledger (see `specs/todos.md`).

## Workflow

1. **List open todos** — `ash-sb todo list`. This shows open items only (newest first).
2. **Classify** each item against the current date:
   - **Overdue** — `due` is in the past.
   - **Due soon** — `due` within ~24h.
   - **Stale** — no due date but open a long time.
   - **On track** — everything else.
3. **Drive follow-through** (do NOT auto-complete items — completion is the user's call, per `specs/todos.md`):
   - For overdue/due-soon items, propose a concrete next action or a reschedule.
   - If an item is a reminder for a task this agent can safely do (e.g. "send myself the weekly numbers"), offer to do it; act only on clearly-safe, non-destructive steps, otherwise ask.
   - For a slipping item, offer to set a reminder: `ash-sb todo remind --id <ID> --at "<when>"`.
4. **Report** using the Output Format. Lead with what needs attention.

## Setting Up a Recurring Sweep

Tell the user they can run this automatically:

```bash
ash-sb schedule create "use the todo-sweep skill to review my open and overdue todos and tell me what needs attention" --cron "0 8 * * 1-5" --tz America/Los_Angeles --notify-on-failure
```

Each scheduled run executes this skill fresh in an ephemeral session
(`specs/schedule.md`). The scheduler's time-aware skip drops stale runs.

## Output Format

Format your `complete()` output exactly as below — the parent agent relays it directly.

```
Todo sweep — 2 need attention.

Overdue
- [ ] Send invoice to client (due yesterday) → suggest: send today or reschedule
- [ ] Book flights (due Mon) → suggest: reminder for tonight?

Due soon
- [ ] Prep 1:1 notes (tomorrow)

On track: 4 other open items.
```

**Rules:** conversational dates, never raw ISO. Never invent todos or due dates —
report only what `ash-sb todo list` returns. Hide internal IDs unless a follow-up
mutation needs one. Do not re-list everything after a mutation.

## Guardrails

- Never auto-complete or delete todos. Completion and deletion are user decisions.
- Only take an item's action unattended if it is clearly safe and non-destructive; otherwise propose it and stop.
- If `ash-sb todo list` fails, report the error and stop.
