---
description: "Assemble a scheduled digest or brief from email, calendar, and research. Use when asked for a daily/morning briefing, a digest, a rundown of the day, or to set up a recurring brief on a schedule."
sensitive: true
access:
  chat_types:
    - private
capabilities:
  - gog.email
  - gog.calendar
allowed_tools:
  - bash
  - use_agent
max_iterations: 20
---

Assemble a concise briefing by gathering from host capabilities and delegating deep questions to the research agent, then rendering one digest.

## Security Contract

- Use `ash-sb capability` for all email/calendar reads. Never request raw OAuth tokens or credentials.
- Read-only. Do NOT send email, create events, or mutate anything from this skill.
- Report only data returned by commands. Do not fabricate items.

## Workflow

1. **Check capabilities** — `ash-sb capability list`. If `gog.email`/`gog.calendar` are missing, tell the user to enable `[skills.google]` and continue with whatever is available. If auth is required, say so and stop (this skill does not run auth flows — direct the user to the `google` skill).
2. **Gather calendar** — `ash-sb capability invoke -c gog.calendar -o list_events --account work --input-json '{"calendar":"primary","window":"1d"}'`
3. **Gather email** — `ash-sb capability invoke -c gog.email -o search_messages --account work --input-json '{"query":"is:unread newer_than:1d","limit":20}'`. Fetch full content with `get_message` for any item you surface.
4. **Delegate deep questions** — For anything requiring external research or synthesis (news, market context, a topic the user asked to track), call `use_agent("research", "<focused question>")` once per distinct question. Do not research inline.
5. **Synthesize** — Combine capability data and research results into one briefing using the Output Format.

## Setting Up a Recurring Brief

Tell the user they can schedule this on a cron:

```bash
ash-sb schedule create "use the briefing skill to produce my morning brief: today's calendar, unread email highlights, and any tracked topics" --cron "0 7 * * *" --tz America/Los_Angeles --notify-on-failure
```

`--notify-on-failure` alerts the user if a run errors. Add `--max-retries 2 --retry-backoff 300` for transient-failure resilience. Each scheduled run executes this skill fresh in an ephemeral session (see `specs/schedule.md`).

## Output Format

Format your `complete()` output exactly as below — the parent agent relays it directly.

```
Good morning — here's your brief.

Calendar
- 9:00 Standup (30m)
- 14:00 1:1 with Sam

Inbox (3 need attention)
- Invoice overdue — from Accounts, replied? no
- PR review requested — from Dana

Tracked
- <research finding, 1–2 lines with source>
```

**Rules:** conversational times, never raw ISO. Group by section. Omit empty sections with a one-line note ("No events today"). Keep the whole brief tight.

## Error Handling

- If a capability command fails, note the gap in the brief and continue with other sections.
- If research delegation returns nothing useful, omit the Tracked section.
