---
description: Explicit slash-command entrypoint for the DeepAgents-backed research flow
triggers:
  - /research
  - /research-smoke
  - /research-demo
  - /research-full
allowed_tools:
  - use_agent
max_iterations: 6
---

You were invoked by an explicit slash command. This skill exists so Ash only runs the heavy research flow when the user explicitly asks for it with `/research`, `/research-smoke`, `/research-demo`, or `/research-full`.

## Goal

Call the built-in `research` agent and return the agent result. The host integration will send the generated markdown report file back to the user automatically when available.

## Input Interpretation

- The skill message is the text that followed `/research`.
- If the user provided no text after `/research`, stop and tell them to provide a research question.
- Preserve the user's wording for the research question.

## Mode Resolution

- If the invocation context mentions `/research-smoke`, set `mode="smoke"`.
- If the invocation context mentions `/research-demo`, set `mode="demo"`.
- If the invocation context mentions `/research-full`, set `mode="full"`.
- Otherwise default to `mode="demo"`.

## Defaults

- Default to `codex_review=true`.
- Default to `email_results=false`.

## Simple Parsing Rules

- If the slash command already fixed the mode, do not override it from the message body.
- Otherwise, if the user text mentions `smoke mode` or starts with `smoke:`, set `mode="smoke"`.
- Otherwise, if the user text mentions `full mode` or starts with `full:`, set `mode="full"`.
- Otherwise, if the user text mentions `demo mode` or starts with `demo:`, set `mode="demo"`.
- If the user text mentions `no codex review`, `without codex review`, or `codex off`, set `codex_review=false`.

## Action

Invoke:

```json
{
  "agent": "research",
  "message": "<user research question>",
  "input": {
    "mode": "<resolved mode>",
    "codex_review": <true|false>,
    "email_results": false
  }
}
```

After the research agent returns:

Return the agent result as your final answer with no extra commentary.
