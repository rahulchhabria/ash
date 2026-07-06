---
description: "Self-re-arming monitor: check a condition (build, PR, metric, page), report only on meaningful change, then reschedule the next check. Use to watch/poll something until it changes, or to keep an eye on a status over time."
allowed_tools:
  - bash
max_iterations: 15
---

Monitor loop: evaluate a condition once, report only if something meaningful changed since last time, then re-arm the next check by scheduling this skill again. Designed to be launched from a scheduled run (see `specs/schedule.md`).

## Time-Aware Skip

Scheduled runs arrive wrapped with timing context (`<timing>`, `<decision-guidance>`). Honor it:

- A monitor check is **time-independent** — a build/PR/metric status is valuable whenever it runs — so normally execute even if delayed.
- But if a much fresher check would run imminently (this run is stale and the next arming is close), skip the stale check to avoid duplicate/misleading reports, per `specs/schedule.md`. Say briefly that you skipped and when the next check runs.

## Workflow

1. **Check the condition** — run the read command for what you're watching (e.g. `gh pr checks`, a curl to a status endpoint, a metric query). Read-only.
2. **Compare to last known state** — the scheduled task message should carry the last observed value (see re-arm step). If the current value equals the last, this is a no-change tick.
3. **Report only on meaningful change** — if changed (or first observation, or a terminal state like success/failure), report it. If unchanged, stay silent: return a terse no-op via `complete()`.
4. **Re-arm** — schedule the next check, embedding the current value so the next run can compare:

```bash
ash-sb schedule create "use the watch skill to check <thing>; last value was <current_value>" --at "in 15 minutes" --notify-on-failure
```

Stop re-arming once a terminal condition is reached (build passed/failed, PR merged) — report that and do not reschedule.

## Cron vs Self-Rearm

- **Fixed cadence** → advise the user to set a cron once (`--cron "*/15 * * * *"`) rather than self-rearming.
- **Dynamic cadence** (back off when quiet, tighten near an event) → self-rearm with `--at` and adjust the interval each tick. Add `--max-retries 2 --retry-backoff 300` for transient failures.

## Output Format

Format your `complete()` output exactly as below.

**On meaningful change:**
```
Build main: FAILING (was passing) — 2 checks red: lint, e2e. Next check in 15m.
```

**Terminal — stop:**
```
PR #482 merged. Stopping the watch.
```

**No change (silent tick):**
```
No change (build still passing). Next check in 15m.
```

**Skipped stale run:**
```
Skipped stale check — next scheduled run is imminent.
```

## Guardrails

- Read-only monitoring; never mutate the thing you're watching.
- Always either re-arm or explicitly stop — never leave the watch dangling.
- Report only on change to avoid notification spam.
