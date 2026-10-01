# Email Forward Summary Integration

## Purpose

Bridge the workspace `email-forward-summary` skill with the ash agent so
the user can hold follow-up conversations about a specific forwarded
school email directly in Telegram (by replying to the bot summary).

## Contract

- Integration name: `email_forward_summary`
- Priority: `170` (runs before memory and todo, after image preprocessing)
- Surface: `preprocess_incoming_message` only
- Config: `[email_forward_summary]` in `~/.ash/config.toml`

```toml
[email_forward_summary]
enabled = true
database_path = "/home/<user>/.ash/workspace/skills/email-forward-summary/data/school_email_pipeline.sqlite3"
max_body_chars = 4000
```

## Behavior

- Delivered email summaries MUST be appended to chat history with the exact sent text,
  delivery timestamp, external message ID, thread ID, and `source_id: email:<id>`.
- Delivery registration MUST be idempotent by external message ID.
- Retained legacy focus summaries MUST be recovered into chat history once, preserving
  their original timestamp and marking them as recovered summaries (not exact sent text).
- Explicit replies resolve their email source first. Replies to subsequent assistant
  messages and non-reply continuations resolve the conversation thread's source from
  that chat's history, including after focus expiry or a restart.
- Thread source resolution MUST NOT require the follow-up to repeat the email subject
  or contain a pronoun. For example, “what are the dates i should care about” continues
  the current email conversation.
- A new-topic request MUST NOT inject the previous email source.
- If there is no thread source, keyword focus fallback may select only one unambiguous,
  unexpired candidate. Multiple matching candidates MUST NOT silently choose one.
- Source recovery MUST be scoped to the current chat's persisted delivery record.
- Context policy checks apply before context injection; disabled integrations do not
  recover or inject context.
- Resolved context includes subject, sender, received timestamp, structured calendar
  and action fields, and body truncated to `max_body_chars`. The incoming message retains
  `email_forward_summary.email_id`, `.subject`, and `.source` metadata.

## Isolation

- All DB access uses `sqlite3` with `mode=ro` URI to guarantee no writes.
- SQL errors are logged at `WARNING` with a `email_forward_summary_lookup_failed`
  event and the message is returned unchanged.

## Logging

| Event                                       | Level   | Notes |
| ------------------------------------------- | ------- | ----- |
| `email_forward_summary_ready`               | INFO    | At setup when enabled + DB present |
| `email_forward_summary_disabled`            | WARNING | At setup when config invalid |
| `email_forward_summary_context_injected`    | INFO    | Per matching reply |
| `email_forward_summary_lookup_failed`       | WARNING | On `sqlite3.Error` |

## Tests

`tests/test_email_forward_summary_integration.py` covers:

- Context injection on matched reply
- Disabled when `enabled = false`
- Disabled when DB path missing / file absent
- Pronoun-free follow-up resolves the current thread's email
- Thread source survives focus expiry and process restart
- Legacy summary recovery is idempotent and preserves timestamps
- Unrelated chats and explicit new topics do not inherit source context
- Ambiguous keyword matches do not pick an arbitrary email
- No-op when neither reply nor conversation has a known source
- Body truncation honors `max_body_chars`
