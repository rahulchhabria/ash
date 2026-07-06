---
description: "Route long-horizon, multi-step work to a focused subagent instead of running it in the capped main loop. Use when a task needs sustained research, deep investigation, or a full implementation plan that would exhaust the main agent's iterations."
allowed_tools:
  - use_agent
max_iterations: 10
---

Decide whether a task should be delegated to a built-in agent, then frame and hand it off. Delegation moves long work into an isolated loop with its own iteration budget, keeping the main conversation responsive (see `specs/interactive-agents.md`).

## When to Delegate vs Handle Inline

**Delegate** when the task is:
- Multi-step and long-horizon (many searches, files, or reasoning passes).
- Open-ended research or synthesis across sources.
- A full implementation/design plan.
- Likely to blow the main loop's iteration cap.

**Handle inline** (do NOT invoke an agent) when the task is:
- A single lookup, quick edit, or one-shot answer.
- Already scoped down to a couple of steps.

## Choosing the Agent

| Agent | Use for |
|-------|---------|
| `research` | External/web research, source-backed reports (DeepAgents-backed). |
| `deep` | Long-horizon, deeply iterative investigation or multi-phase work. |
| `plan` | Architecture and implementation plans (the architect). |
| `task` | General multi-step worker for everything else. |

## Workflow

1. **Triage** — if the task is inline-sized, say so and return without delegating.
2. **Pick one agent** from the table by the dominant need.
3. **Frame the task** — write a self-contained brief: the goal, known constraints/context, and the exact deliverable shape you want back. A vague hand-off wastes the subagent's budget.
4. **Delegate** — `use_agent("<agent>", "<framed brief>")`. Pass structured extras via the `input` field when the agent supports them (e.g. research `mode`).
5. **Relay** — return the agent's result, lightly framed, via `complete()`. Do not silently re-do the work.

## Framing Template

```
Goal: <one sentence>
Context: <constraints, prior decisions, links>
Deliverable: <format — e.g. "ranked list with sources", "step-by-step plan", "report">
```

## Output Format

Format your `complete()` output exactly as below.

```
Delegated to <agent>: <goal>

<the agent's result, relayed>
```

If handled inline instead:

```
No delegation needed — <one-line reason>.
```

## Guardrails

- Skills cannot call `use_skill`; only `use_agent` is available here.
- Delegate to exactly one agent per hand-off; use the `fan-out` skill for parallel decomposition.
- Never fabricate a result if the agent returns empty — report that plainly.
