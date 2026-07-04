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

When ash receives an `IncomingMessage` from any provider:

1. If the integration is disabled, the SQLite path is unset, or the file
   does not exist, return the message unchanged.
2. If the message has no `reply_to_message_id`, return unchanged.
3. Coerce `reply_to_message_id` to `int`; on failure, return unchanged.
4. Query the email pipeline DB in read-only mode:

   ```sql
   SELECT id, subject, sender, received_at, cleaned_body,
          structured_parse_json, processing_status
   FROM emails
   WHERE telegram_message_id = ?
   ORDER BY id DESC LIMIT 1
   ```

5. If no row matches, return unchanged.
6. Otherwise prepend a structured context block to `message.text`:

   ```
   --- Email-forward-summary context (reply target) ---
   email_id: <id>
   subject: <subject>
   from: <sender>
   received_at: <received_at>
   structured_summary:
   <slim JSON of selected parse fields>
   body:
   <cleaned_body, truncated to max_body_chars>
   --- End email-forward-summary context ---
   ```

7. Set `message.metadata["email_forward_summary.email_id"]` and
   `message.metadata["email_forward_summary.subject"]`.

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
- No-op when no `reply_to_message_id`
- No-op when `reply_to_message_id` does not match any email
- Body truncation honors `max_body_chars`
