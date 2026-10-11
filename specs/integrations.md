# Integrations

Integration contributors extend runtime behavior through deterministic hooks.

## Durable background delivery

Integrations that launch user-visible background work MUST persist a pending delivery
record before starting an in-process task. Their `on_startup` hook MUST resume pending
records in server mode, and successful delivery MUST durably mark the record complete
before it is excluded from later recovery. Their `on_shutdown` hook may cancel runtime
tasks, but cancellation MUST leave undelivered records pending.

Vapi call summaries follow this contract through `VapiCallsIntegration`: tool
registration is integration-owned, call operations persist the destination, objective,
Telegram chat, business name, and summary-delivery status, and server startup recreates
watchers only for records whose delivery status is `pending`. An accepted call is
persisted before the call-creation HTTP client yields or closes. Summary delivery is
marked `pending` only when both a Telegram token and destination are available;
otherwise it is marked `disabled`. Transient Telegram delivery failures retry with
bounded exponential backoff and remain durable until delivery succeeds.

## Contributor Template

Use `IntegrationContributor` and implement only hooks your feature needs:

```python
from ash.integrations.runtime import IntegrationContributor, IntegrationContext


class ExampleIntegration(IntegrationContributor):
    name = "example"
    priority = 500

    async def setup(self, context: IntegrationContext) -> None:
        ...

    def register_rpc_methods(self, server, context: IntegrationContext) -> None:
        ...

    def augment_prompt_context(self, prompt_context, session, context):
        return prompt_context

    def augment_skill_instructions(self, skill_name, context):
        return []

    def augment_sandbox_env(self, env, session, effective_user_id, context):
        return env

    async def preprocess_incoming_message(self, message, context: IntegrationContext):
        return message

    async def on_message_postprocess(
        self,
        user_message: str,
        session,
        effective_user_id: str,
        context: IntegrationContext,
    ) -> None:
        ...
```

## Rules

### Conversation observability

`BraintrustIntegration` owns optional post-turn conversation observations and its
bounded export queue. It is composed in chat and serve modes; eval results use
explicit Braintrust experiments instead of production logs. Disabled by default.
Credentials resolve from the host `BRAINTRUST_API_KEY` and must never be projected
into sandbox environment hooks. Content capture requires `capture_content = true`.
Captured messages are bounded, scrubbed for common credential patterns and email
addresses, and marked when truncated; this is not a guarantee of anonymization.

Post-turn observations contain session/conversation ID, tool calls/results, model,
release and Sentry trace ID when available. They describe retained session state,
not guaranteed delivered replies or full replays: compaction, steering, exceptions,
interactive children and provider finalization can change or omit context. Sentry
remains the source for live timing, LLM spans and exceptions. Mechanical tool-error
counts must not be called accuracy scores. Export failures must never fail a turn.
The queue drops new observations when full, with an operational warning; shutdown
drains with a bounded wait. Observability must not mutate prompts or agent behavior.

`evals run --braintrust-project ash` publishes existing eval judgments as a named
experiment. Judge errors remain explicitly unknown (no quality score), and fail
the run. Export failures and zero matched cases fail explicitly requested exports.
Normal eval runs do not require Braintrust. Neither observations nor an eval pass
authorize autonomous code changes or establish production recovery.

1. Set stable `name` and `priority`; runtime ordering is `(priority, name)`.
2. Keep hook behavior local to the integration domain.
3. Post-turn behavior belongs in `on_message_postprocess`, not provider/core call sites.
4. Pre-turn inbound transformations belong in `preprocess_incoming_message`.
5. Register via shared composition (`create_default_integrations` + `compose_integrations`).
6. Hook failures must be isolated per contributor and logged with hook + contributor metadata.
7. Contributors that fail in `setup` are excluded from later hook/lifecycle execution.
8. If an integration introduces graph-backed entities, it owns registration of node collections and edge schemas via graph extension APIs before use.
9. Integration contributors are trusted first-party runtime capabilities, not third-party plugin points.
10. Third-party extensions must go through skills/capability surfaces rather than registering integration contributors directly.
11. Runtime-scoped sandbox env data (for example RPC transport hints) must flow through `IntegrationContext.sandbox_env` and integration env hooks, not process-global environment mutation.

## Integration-Provided Skills

Integrations can provide skills from a co-located `skills/` directory at
`src/ash/integrations/skills/{contributor_name}/{skill_name}/SKILL.md`.

These are loaded during `SkillRegistry.discover()` at integration precedence
(above bundled, below installed/user/workspace) and gated on the same
`include_bundled` flag as built-in skills.

Layout:
```
src/ash/integrations/skills/
  todo/           # contributor (matches integration name)
    todo/         # skill name
      SKILL.md
  browser/
    screenshot/
      SKILL.md
```

Each contributor directory is iterated in sorted order. Skill files follow
the standard `SKILL.md` frontmatter format used by all other skill sources.

### Container Mount Paths

Skills are mounted read-only in the sandbox container at predictable paths:

```
/ash/skills/{skill_name}/                                    # bundled (ro)
/ash/integrations/{contributor}/skills/{skill_name}/          # integration (ro)
/workspace/skills/{skill_name}/                              # workspace (rw)
```

The system prompt includes the sandbox path for each skill so the agent can
locate co-located files. The `ASH_SKILL_DIRS` env var (colon-separated) lists
all mounted skill directories for `ash-sb skill list` discovery.

## Testing Checklist

1. Add unit tests for hook behavior.
2. Add runtime integration tests for ordering and side effects.
3. Add/update architecture guards when ownership boundaries change.

## Dependency readiness

Integrations may register bounded async health probes with the runtime. `/health`
is process liveness; `/ready` checks integration failures and registered dependency
probes, returning HTTP 503 if a required check fails. The RPC lifecycle owns the
sandbox-to-host socket probe, keeping feature checks out of server wiring.
