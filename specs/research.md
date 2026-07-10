# Research Agent

> Built-in agent for DeepAgents-backed research with optional GLiNER extraction and Codex review

Files: src/ash/agents/builtin/research.py, src/ash/research/service.py, src/ash/cli/commands/research.py

## Requirements

### MUST

- Be invocable via `use_agent` tool with agent="research"
- Run as a passthrough agent rather than an LLM tool loop
- Create a timestamped job directory under `~/.ash/research/jobs/`
- Persist `request.md`, `report.md`, `brief.md`, `facts.json`, and `metadata.json`
- Return structured artifact metadata from the research agent result so integrations can deterministically deliver the generated report without requiring an LLM to scrape paths from prose
- Attempt to run a local DeepAgents research workflow when available
- Degrade gracefully when DeepAgents is unavailable by writing a fallback report
- Attempt GLiNER extraction over the normalized research artifacts
- Support optional final review using an Ash model alias such as `codex`
- Expose a direct CLI entrypoint via `ash research`

### SHOULD

- Copy backend notes, transcript, and sources into the Ash job directory when available
- Support `mode` values `smoke`, `demo`, and `full`
- Support DeepAgents model and max-search-result overrides

### MAY

- Include actionable next steps in `actions.json`
- Support custom GLiNER label schemas per request

## Interface

```python
use_agent(
    agent="research",
    message="Research modern AI agent architectures",
    input={
        "mode": "demo",
        "deepagents_model": "gpt-5.2-codex",
        "max_search_results": 5,
        "codex_review": True,
    },
)

ash research "Research modern AI agent architectures" --mode demo
```

## Behaviors

| Input | Output | Notes |
|-------|--------|-------|
| Message only | Job directory + brief/report paths | Uses default `demo` mode |
| `use_agent("research", ...)` in provider flows | Structured metadata includes `document_path` for `report.md` | Provider integration may auto-send the report artifact |
| DeepAgents available | Copied backend report + notes + sources | Normalized into Ash artifact layout |
| DeepAgents unavailable | Fallback report | No external research performed |
| GLiNER available | `facts.json` with extracted entities | Operates on combined report/source text |
| Codex alias available | `brief.md` and `actions.json` | Uses Ash model config |

## Errors

| Condition | Response |
|-----------|----------|
| Empty message | `ValueError("question is required")` |
| DeepAgents runner missing | Fallback report with backend error |
| DeepAgents timeout | Partial/failed run with timeout recorded in metadata |
| GLiNER failure | `facts.json` records extractor unavailability/failure |
| Codex review unavailable | Brief falls back to report excerpt |

## Verification

```bash
uv run ash research "Compare Sentry and Honeycomb for AI debugging relevance" --mode smoke
uv run ash chat "Use the research agent to compare Sentry and Honeycomb"
```

- `use_agent` can invoke `research`
- `ash research` creates a timestamped job directory
- `report.md`, `brief.md`, `facts.json`, and `metadata.json` are written
- When DeepAgents is configured locally, backend artifacts are copied into the Ash job
