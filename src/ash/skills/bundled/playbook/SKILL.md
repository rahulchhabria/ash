---
description: "Scaffold that turns a repeated multi-step procedure into a reusable autonomous skill. Use when the user wants to automate a routine they do often, or asks how to capture a workflow as a skill."
allowed_tools:
  - bash
max_iterations: 15
---

Capture a repeated procedure as a new first-party skill. This skill is both a worked example and the pattern to follow. For creating the skill file interactively, defer to `/create-skill`; use this when you want the pattern and a template in one place.

## The Pattern

A good autonomous skill is a procedure written as steps an isolated subagent can follow with no conversation context:

1. **Name the trigger** — what phrasing should route here? Put those phrases in `description` (skill selection matches on it).
2. **List the primitives** — which host surfaces does it touch? `ash-sb capability` (email/calendar), `ash-sb schedule` (timed/recurring), `ash-sb todo` (tasks), `use_agent` (delegation). Declare `capabilities:` and `allowed_tools:` for exactly those.
3. **Write the steps** — a numbered, self-contained workflow. Assume none of the current chat is visible at run time.
4. **Set guardrails** — mark `sensitive: true` + `access.chat_types: [private]` for anything touching personal data; require confirmation before mutations.
5. **Define `complete()` output** — the exact shape the parent agent relays.

## Placement & Validation

- Workspace skills live at `workspace/skills/<name>/SKILL.md` (highest precedence — see `specs/skills.md`). No manifest or registration step; discovery scans the directory.
- Validate the file parses: `ash-sb skill validate workspace/skills/<name>/SKILL.md`.

## Worked Example — "weekly-report"

A user runs the same Friday routine: pull the week's merged PRs, summarize, file a follow-up todo. Captured as a skill:

```yaml
---
description: "Compile a weekly engineering report. Use for a Friday wrap-up or week-in-review of shipped work."
allowed_tools:
  - bash
  - use_agent
max_iterations: 20
---

1. Gather merged PRs for the week via `gh pr list --state merged --search "merged:>=<date>"`.
2. For deep context on a large PR, delegate with `use_agent("research", "<focused question>")`.
3. Summarize into themes (shipped, in-flight, risks).
4. File next week's follow-ups: `ash-sb todo add "<item>"` per open thread.
5. complete() with the report in the Output Format below.
```

Schedule it: `ash-sb schedule create "use the weekly-report skill" --cron "0 16 * * 5" --tz America/Los_Angeles --notify-on-failure`.

## Workflow (when invoked)

1. Ask (or infer) the procedure's trigger phrases, steps, and which primitives it needs.
2. Draft frontmatter (`description`, `allowed_tools`, `capabilities`/`sensitive` as needed, `max_iterations`).
3. Draft the numbered body + a `complete()` output block, mirroring the example.
4. Write it to `workspace/skills/<name>/SKILL.md` and run `ash-sb skill validate` on it.
5. Suggest a schedule if the procedure is recurring.

## Output Format

Format your `complete()` output exactly as below.

```
Created skill: <name> at workspace/skills/<name>/SKILL.md
Triggers on: <phrases>
Uses: <primitives>
Validated: OK
Suggested schedule: <cron or "on demand">
```

## Guardrails

- Only write files under `workspace/skills/`. Never touch Python source, integrations, or core code.
- Reuse existing skills (`briefing`, `triage`, `watch`, `delegate-deep`, `fan-out`) rather than duplicating them.
- Keep the generated skill tight — steps, guardrails, output format; no filler.
