---
description: "Decompose a goal into independent subtasks, run them across parallel subagents, then synthesize the results. Use for compare/contrast research, multi-topic gathering, or any goal that splits cleanly into pieces that don't depend on each other."
allowed_tools:
  - use_agent
max_iterations: 20
---

Coordinator pattern: break a goal into independent subtasks, dispatch each to a built-in agent, and combine the returns into one answer. This parallelizes work across isolated loops (see `specs/interactive-agents.md`).

## When to Use

- The goal splits into pieces that can be worked **independently** (no piece needs another's output).
- Examples: "compare 4 vendors", "gather X for each of these 5 repos", "research these 3 angles".

If subtasks are sequential (each depends on the last), do NOT fan out — chain them or use `delegate-deep`.

## Workflow

1. **Decompose** — write 2–6 self-contained subtasks. Each must stand alone (full context, explicit deliverable). Name the synthesis you'll produce at the end.
2. **Assign agents** — pick per subtask: `research` (source-backed web work), `task` (general worker), `plan` (design/architecture). Different subtasks may use different agents.
3. **Dispatch** — call `use_agent(...)` for each subtask. Issue the calls back-to-back so the orchestrator runs them as separate child frames; collect every result before synthesizing.
4. **Synthesize** — merge results into one structured output: reconcile overlaps, note disagreements between subagents, and answer the original goal. Do not just concatenate raw returns.

## Sequencing Notes

- Fan-out is for **independent** branches. If a late step needs earlier output, run that step after collecting, as a follow-up delegation.
- Keep the branch count small (2–6). More branches means more budget and a harder synthesis.
- If one branch fails or returns empty, synthesize from the rest and flag the gap.

## Output Format

Format your `complete()` output exactly as below.

```
<goal> — synthesized from N branches

<combined answer, organized by theme not by branch>

Branch notes
- <subtask 1>: <one-line takeaway>
- <subtask 2>: <one-line takeaway>
- <subtask 3>: no result (flagged)
```

## Guardrails

- Skills cannot call `use_skill`; use `use_agent` only.
- Every subtask brief must be self-contained — a child agent sees none of this conversation.
- Always synthesize; never hand back N disconnected reports.
