# Close Game Alert Integration

## Purpose

Bridge the workspace `close-game-alert` and `valkyries-close-game-alert`
skills (and any future `*-close-game-alert` daemons) with the ash agent
so the user can hold follow-up conversations about a recently delivered
close-game alert without having to repeat which game they mean.

The daemons send Telegram alerts directly via the Bot API; those
messages never enter Ash's per-session context. When the user replies
later (often without using Telegram's reply gesture) Ash starts a fresh
session and has no idea what game the user is asking about. This
integration injects a short context block describing the most recent
close-game alert delivered to the chat, so the agent can pick the right
skill and answer questions like "what's the score now?".

## Contract

- Integration name: `close_game_alert`
- Priority: `175` (immediately after `email_forward_summary` at `170`)
- Surface: `preprocess_incoming_message` only
- Config: `[close_game_alert]` in `~/.ash/config.toml`

```toml
[close_game_alert]
enabled = true
recent_window_minutes = 240
history_lookback = 10
alert_prefixes = ["Close Game Alert"]
```

## Daemon contract

For the integration to find an alert, the close-game-alert daemons must
record each outbound alert as an `assistant` entry in the chat-level
`history.jsonl` for the destination chat (see `specs/sessions.md`).
Concretely:

- Path: `~/.ash/chats/{provider}/{chat_id}/history.jsonl`
- Required fields: `id`, `role: "assistant"`, `content`, `created_at`
- Recommended metadata: `{"source": "<skill-name>", "external_id": "<telegram_message_id>"}`
- The first non-whitespace characters of `content` must match one of the
  configured `alert_prefixes` (default: `Close Game Alert`).

## Behavior

When ash receives an `IncomingMessage` from any provider:

1. If the integration is disabled, return the message unchanged.
2. Read up to `history_lookback` entries from the chat's
   `history.jsonl` (using `read_recent_chat_history`).
3. Walk the entries newest-first. The first `assistant` entry whose
   trimmed content starts with any configured prefix and whose
   `created_at` falls within `recent_window_minutes` is selected.
4. If no entry matches, return the message unchanged.
5. Otherwise prepend a structured context block to `message.text`:

   ```
   --- Close-game-alert context (recent alert) ---
   sent_at_utc: <ISO-8601>
   source: <skill-name, if recorded>
   alert_message:
   <full alert text>
   guidance: ...
   --- End close-game-alert context ---
   ```

6. Set `message.metadata["close_game_alert.alert_id"]` and
   `message.metadata["close_game_alert.alert_created_at"]`.

## Isolation

- The integration only reads the chat-level history file; it never
  writes to it. Failures during read are logged at WARNING and the
  message is returned unchanged.
- Live game-state retrieval is delegated to the close-game-alert skills
  via the agent's normal skill flow; this integration only injects
  context.

## Logging

| Event                                  | Level   | Notes |
| -------------------------------------- | ------- | ----- |
| `close_game_alert_ready`               | INFO    | At setup when enabled |
| `close_game_alert_disabled`            | WARNING | At setup when prefixes empty |
| `close_game_alert_context_injected`    | INFO    | Per matching incoming message |
| `close_game_alert_history_read_failed` | WARNING | On chat-history read errors |

## Tests

`tests/test_close_game_alert_integration.py` covers:

- Context injection when a recent alert is present in history
- No-op when integration disabled
- No-op when no matching alert in history
- No-op when matching alert is older than `recent_window_minutes`
- Honors `alert_prefixes` when matching alert content
