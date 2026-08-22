"""Coding agent for Telegram-first repo work."""

from ash.agents.base import Agent, AgentConfig, AgentContext

CODING_SYSTEM_PROMPT = """You are Ash's coding harness agent. You turn a chat request into a controlled code change.

## Operating Loop

1. Create or update a `coding_job` for the task.
2. Inspect repo state with `repo` and read the relevant files before editing.
3. Make changes with `apply_patch` whenever possible. Use `write_file` only for brand-new files or generated artifacts.
4. Run focused tests with `repo(action="test")` or `bash` when needed.
5. Review the final diff with `repo(action="diff")` and summarize files changed, tests run, residual risks, and next actions.

## Safety Gates

Ask the user before destructive or externally visible actions: force pushes, deploys, migrations, deleting user data, changing secrets, or broad dependency upgrades. Use `interrupt` with clear options when approval is needed.

## Telegram UX

Keep progress terse. Prefer `send_message` for long-running status only. Final responses must include the coding job id, changed files, test result, and any approval needed.

## Tool Policy

- Prefer `repo` over raw git shell commands because its output is self-verifying.
- Prefer `apply_patch` over `write_file` for edits.
- Use `web_search` or hosted search tools for current package/API facts.
- Use `use_agent` to delegate independent review or research when helpful.
"""


class CodingAgent(Agent):
    """Repo-oriented coding harness agent."""

    def __init__(self, model_alias: str | None = None) -> None:
        self._model_alias = model_alias

    @property
    def config(self) -> AgentConfig:
        return AgentConfig(
            name="coding",
            description="Plan, edit, test, review, and summarize code changes from Telegram or CLI.",
            system_prompt=CODING_SYSTEM_PROMPT,
            allowed_tools=[
                "coding_job",
                "repo",
                "read_file",
                "write_file",
                "apply_patch",
                "bash",
                "web_search",
                "web_fetch",
                "openai_web_search",
                "openai_file_search",
                "use_agent",
                "interrupt",
                "send_message",
            ],
            model=self._model_alias,
            max_iterations=40,
            supports_checkpointing=True,
            timeout=1800,
        )

    def _build_prompt_sections(self, context: AgentContext) -> list[str]:
        sections = []
        repo_path = context.input_data.get("repo_path") or "/workspace"
        sections.append(
            "## Coding Harness Context\n\n"
            f"- Repo path: `{repo_path}`\n"
            f"- Chat provider: `{context.provider or 'unknown'}`\n"
            f"- Session: `{context.session_id or 'unknown'}`\n"
            "- Use a branch for non-trivial changes when the repo allows it.\n"
            "- Treat uncommitted changes as user-owned unless you made them in this job."
        )
        job_id = context.input_data.get("job_id")
        if job_id:
            sections.append(f"## Existing Coding Job\n\nContinue coding job `{job_id}`.")
        return sections
