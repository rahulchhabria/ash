# Pigeon

A personal agent for Telegram-dispatched research, browser work, phone inquiries,
school-email triage, memory, and sandboxed tool execution.

The Python package, `ash` command, and `~/.ash` state directory remain available
as compatibility interfaces. New command-line usage should use `pigeon`.

## Features

- **Customizable Personality**: Define your assistant's behavior via SOUL.md
- **Sessions**: JSONL-based conversation history
- **Memory**: SQLite-backed semantic search for extracted facts
- **Multi-LLM Support**: Anthropic Claude and OpenAI
- **Telegram Integration**: Chat with your assistant via Telegram
- **Sandboxed Tools**: Execute bash commands in Docker containers
- **Web Search**: Built-in Parallel Search integration

## Documentation

Full documentation at **https://dcramer.github.io/ash/**

## Development

```bash
make setup  # Install deps + prek hooks
```

| Command | Purpose |
|---------|---------|
| `make lint` | Run ruff linting and formatting |
| `make typecheck` | Run ty type checker |
| `make test` | Run pytest |
| `make check` | Run all hooks |

### Evals

Behavior evaluations use LLM-as-judge to test agent responses. They require `ANTHROPIC_API_KEY`:

```bash
uv run pytest evals/ -m eval -v
```

Eval cases live in `evals/cases/*.yaml`. See `evals/types.py` for the schema.

## Claude Code

This project is built with [Claude Code](https://claude.com/code). Agent instructions live in `CLAUDE.md`.

Install required plugins:

```bash
claude plugin add anthropics/code-simplifier
```

**From [anthropics/code-simplifier](https://github.com/anthropics/claude-plugins-official/tree/main/plugins/code-simplifier)**:

| Agent | Purpose |
|-------|---------|
| `code-simplifier` | Reduce code complexity and remove over-engineering |

**Project-specific skills** (in `.claude/skills/`):

| Skill | Purpose |
|-------|---------|
| `/write-spec <feature>` | Create/update a feature spec |
| `/verify-spec <feature>` | Verify implementation matches spec |
| `/create-migration` | Database schema changes |
| `/create-skill <name>` | Create Pigeon skills in workspace |

## License

MIT

## Sentry and Braintrust

Sentry monitors exceptions, LLM calls and performance. The optional Braintrust
integration records post-turn session snapshots for conversation review. Both use
the Ash session ID as `conversation_id`; Braintrust also records the active Sentry
trace ID when available. Keep Sentry's existing DSN and set its `release` to the
actual deployed revision when releasing a change.

Install with `uv sync --all-groups --extra braintrust`. Add to your Ash config:

```toml
[braintrust]
enabled = true
project = "ash"
capture_content = true
```

Set `BRAINTRUST_API_KEY` in the host service environment (never in `[env]`, which
is available to tools). For a user systemd service, use a mode-0600 environment
file and reference it with `EnvironmentFile=` in an `ash.service` drop-in.
An exported variable in a terminal alone does not update an already running service.
Without `capture_content`, only metadata and mechanical tool counts are exported.
Common credentials and email addresses are filtered, but captured conversations
are not guaranteed anonymous. Messages and individual text fields are bounded.

Records describe retained session state, not necessarily the final delivered
reply. Compaction, interactive children, exceptions and delivery formatting can
omit or change context. This export is not a deterministic replay recorder.
Tool failures are operational signals, not an accuracy score. Sentry remains the
source for duration, cost/token telemetry where available, and exceptions.

To publish an explicitly requested behavioral eval run to Braintrust:

```bash
uv run --extra braintrust evals run system_e2e --braintrust-project ash --experiment baseline
```

This uses real LLM calls. The existing judge and deterministic assertions still
control pass/fail. Judge errors have no quality score and fail the run. Compare a
candidate experiment to a baseline before promoting changes. An export failure
also fails an explicitly requested export. Production exports are best effort:
queue overflow drops records with a warning and cannot block replies.

Initial rollout: verify a synthetic trace, review production observations, label
actual failures, then add them to the eval suites. Configure online Braintrust
scorers against those labels before trusting their judgments. Automated repair
and release/rollback gates are separate work; a successful coding-agent exit
must not be treated as proof that a conversation regression is fixed.
