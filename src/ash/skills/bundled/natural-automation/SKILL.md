---
name: natural-automation
description: Turn natural-language requests like "watch this", "when this happens", "dig into this", and "keep an eye on this" into Pigeon automations using event triggers, polling daemons, and DeepAgents background investigations.
authors:
  - rahul
rationale: User wants to control Pigeon automations entirely with natural language while DeepAgents handles longer analysis and Pigeon remains the trigger, policy, and approval boundary.
allowed_tools:
  - deepagents_status
  - deep_research
  - use_agent
  - read_file
  - bash
triggers:
  - natural automation
  - watch this
  - keep an eye on
  - tell me when
  - when this happens
  - dig into this
  - investigate this
  - research this in the background
  - set up a watcher
  - set up an alert
  - summarize when
  - automation levels
  - level 3 automation
  - level 4 automation
  - level 5 automation
  - deep automation
  - deepagents automation
  - set up automation
max_iterations: 16
---

Use this skill when the user asks in natural language to design, review, or
activate Pigeon automations.

Read `references/playbook.md` before making recommendations.
Use `data/starter_automations.json` as the starting menu unless the user provides
a more specific target.

## Natural Language Mapping

When the user says "when this happens", "if I get", "whenever an email/webhook
arrives", or "summarize incoming", use passive/event intake.

When the user says "watch this", "keep an eye on", "tell me when", "alert me if",
or "check every", use scheduled polling or a schedule-aware daemon.

When the user says "dig into this", "research this", "investigate", "compare",
"review", "figure out why", or "give me a recommendation", use DeepAgents
background investigation through `deep_research` or the `deep` agent.

## Operating Rules

- Keep DeepAgents read/research oriented by default.
- Pigeon owns Telegram, memory, side effects, and approvals.
- Use event intake for incoming events, polling for recurring checks, and
  DeepAgents when analysis needs planning, multiple sources, notes, or a transcript.
- For any action beyond reading/searching/reporting, ask the user before acting.
- For polling, include dedupe and a quiet condition that returns `[NO_REPLY]`.
- For DeepAgents jobs, include the requested deliverable, sources requirement,
  timeout expectation, and whether the result should be emailed or only sent to
  Telegram.

## Suggested Flow

1. Identify the trigger: event, schedule, or manual Telegram phrase.
2. Choose the automation shape:
   - Event intake when something arrives.
   - Watcher/polling when something must be checked repeatedly.
   - Background investigation when the task needs deep synthesis or multi-step work.
3. Pick a starter automation from `data/starter_automations.json`, or draft a new one.
4. State the exact trigger, tool/script/agent, alert condition, suppression rule,
   artifact location, and approval boundary.
5. If the user asks to activate it, run a dry run first when possible and report
   the result before creating or changing a long-running schedule/service.

## Response Shape

When proposing automations, use:

`Automation: <name>`
`Kind: <event intake, watcher, or background investigation>`
`Trigger: <event/schedule/manual phrase>`
`What Pigeon does: <short description>`
`When to alert: <condition>`
`When to stay quiet: <condition>`
`DeepAgents role: <none/read/research/investigate>`
`Approval needed for: <actions>`
`Next step: <dry run, schedule, daemon, or research job>`
