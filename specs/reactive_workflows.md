# Reactive Workflows Integration

> Config-driven signal->workflow routing: match an inbound message, route the turn to a skill/agent.

Files: `src/ash/integrations/reactive_workflows.py`, `src/ash/config/models.py`

## Status: Implemented

## Purpose

The scheduler provides *time-driven* autonomy. Reactive workflows provide the
*event-driven* counterpart: when an inbound provider message matches a
configured rule, a structured instruction block is prepended to the message so
the agent deterministically routes the turn to a named skill or built-in agent.

This is the generic form of the bespoke `email_forward_summary` and
`close_game_alert` integrations — instead of hard-coding one signal, users
declare rules in config.

## Contract

- Integration name: `reactive_workflows`
- Priority: `180` (after the bespoke `email_forward_summary`/`close_game_alert`
  injectors at 170/175 so a specific match wins if both apply)
- Surface: `preprocess_incoming_message` only
- Config: `[reactive_workflows]` in `~/.ash/config.toml`

```toml
[reactive_workflows]
enabled = true

[[reactive_workflows.rules]]
name = "invoice-triage"
match_regex = "(?i)invoice|receipt"
skill = "triage"
instruction = "Extract vendor, amount, and due date; propose next action."
chat_types = ["private"]

[[reactive_workflows.rules]]
name = "deep-research-prefix"
match_prefix = "/research"
agent = "deep"
```

## Behavior

For each inbound `IncomingMessage` when the integration is enabled:

1. If disabled or the message has no text, return unchanged.
2. Evaluate rules in declaration order; the **first** match wins.
   - A rule matches when its `match_prefix` is a leading prefix of the
     (left-stripped) text, OR its `match_regex` searches the text.
   - If a rule declares `chat_types`, the message's `chat_type`
     (from `metadata`) must be in that list; unknown chat type fails closed.
3. On match, prepend a structured context block naming the target
   skill/agent + optional guidance, and set
   `message.metadata["reactive_workflow.rule"]`.
4. The block is advisory — it instructs the agent to route via `use_skill` /
   `use_agent` but tells it to use judgment if the message clearly needs a
   different response.

## Rule Validation (at setup)

A rule is skipped (logged `reactive_workflow_rule_skipped`) when:

- it has neither `match_prefix` nor `match_regex` (`no_matcher`), or
- it has none of `skill` / `agent` / `instruction` (`no_action`), or
- its `match_regex` fails to compile (`invalid_regex`).

If no valid rules remain, the integration stays disabled
(`reactive_workflows_disabled`).

## Architecture

- Prompt augmentation is done by transforming the inbound message text
  (`preprocess_incoming_message`) — the same mechanism the bespoke integrations
  use. No prompt-fragment injection into prompt-building code.
- Registered through the shared composition path
  (`create_default_integrations` in all runtime modes), not ad-hoc wiring.

Spec references:
- `specs/subsystems.md` (Integration Hooks)
- `specs/integrations.md`
- `specs/skills.md`

## Tests

`tests/test_reactive_workflows_integration.py` covers:

- Prefix and regex matches inject the routing block + metadata
- First-match-wins ordering
- `chat_types` gating (allowed, denied, unknown)
- No-op when disabled, no text, or no rule matches
- Invalid/actionless rules skipped at setup
