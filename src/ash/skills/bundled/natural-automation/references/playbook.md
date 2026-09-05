# Natural Automation Playbook

This playbook defines how Pigeon should turn natural-language requests into
automations.

The user should not need to say "Level 3", "Level 4", or "Level 5". Those terms
are implementation categories only.

## "When This Happens": Passive/Event Intake

Use this when data arrives from somewhere else:

- forwarded email
- Telegram mention or reply
- webhook payload
- voicemail/transcript payload
- external service alert

The automation should be cheap, bounded, and mobile-friendly. It should classify
the event, summarize only the useful part, and suppress noise with `[NO_REPLY]`.

Good event intake outputs:

- "worth alerting" summary
- action item extraction
- deadline detection
- routing decision: ignore, summarize, ask user, or launch background investigation

Avoid long research inside the intake step. If the event needs investigation,
launch or recommend a DeepAgents background investigation.

## "Watch This": Scheduled Polling

Use this when Pigeon needs to keep watching something:

- game state
- commute time
- product availability
- service health
- CI or PR status
- recurring email/webhook backlog

Prefer schedule-aware daemons that sleep when nothing can happen. Every watcher
needs:

- source of truth
- polling cadence
- threshold
- dedupe window
- quiet output condition
- dry-run command
- restart/status instructions

Watchers should alert rarely. A good check should return `[NO_REPLY]` when there
is no meaningful change.

## "Dig Into This": DeepAgents Background Investigation

Use this when the work needs a longer loop:

- compare options using sources
- inspect a codebase or skill
- investigate a production/service issue
- produce weekly digests
- synthesize multiple events into actions
- create a plan from ambiguous context

Default DeepAgents posture:

- read/search/fetch/triage only
- write reports and notes as artifacts
- return concise Telegram summary
- ask before side effects

Natural prompt examples:

```text
Pigeon, dig into why the close-game checker is falling back to ESPN and tell me
what to fix.
```

```text
Pigeon, watch Lakers games and tell me only if they are close late.
```

```text
Pigeon, when a school email arrives, summarize it and only alert me if there is
an action item or deadline.
```

Internal DeepAgents prompt template:

```text
Use deep_research for this background investigation.

Goal: <one sentence>
Context: <known facts, trigger source, constraints>
Deliverable: <report/brief/checklist/actions>
Sources: Include source URLs or file paths used.
Quiet rule: If there is nothing meaningful, return [NO_REPLY].
Approval boundary: Do not take actions beyond reading/searching/reporting.
```

## Escalation Pattern

Event arrives.
If trivial, summarize or suppress.
If it requires monitoring, create or suggest a watcher.
If it requires real analysis, launch a background investigation.
If the investigation recommends action, ask the user before execution.
