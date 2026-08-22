"""Coding harness tools for repo-oriented agent workflows."""

from __future__ import annotations

import re
import shlex
from pathlib import PurePosixPath
from typing import Any

from ash.coding import CodingJobStore
from ash.sandbox import SandboxExecutor
from ash.tools.base import Tool, ToolContext, ToolResult
from ash.tools.truncation import truncate_tail


class ApplyPatchTool(Tool):
    """Apply a unified diff inside the mounted workspace."""

    def __init__(self, executor: SandboxExecutor) -> None:
        self._executor = executor

    @property
    def name(self) -> str:
        return "apply_patch"

    @property
    def description(self) -> str:
        return (
            "Apply a unified diff to files in the workspace. Use this for code edits "
            "instead of overwriting whole files. The patch is applied with patch(1)."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "patch": {
                    "type": "string",
                    "description": "Unified diff text to apply from /workspace.",
                },
                "strip": {
                    "type": "integer",
                    "description": "patch -p strip count. Default: 0.",
                    "default": 0,
                    "minimum": 0,
                    "maximum": 3,
                },
                "check": {
                    "type": "boolean",
                    "description": "Only validate the patch without applying it.",
                    "default": False,
                },
            },
            "required": ["patch"],
        }

    async def execute(
        self, input_data: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        patch = str(input_data.get("patch") or "")
        if not patch.strip():
            return ToolResult.error("Missing required parameter: patch")
        if len(patch.encode("utf-8")) > 1_000_000:
            return ToolResult.error("Patch too large; limit is 1 MB")
        strip = int(input_data.get("strip", 0) or 0)
        check = bool(input_data.get("check", False))
        patch_path = f"/home/sandbox/ash-patches/ash-patch-{abs(hash(patch))}.diff"
        write = await self._executor.write_file(patch_path, patch)
        if not write.success:
            return ToolResult.error(f"Failed to stage patch: {write.stderr}")
        flags = f"-p{strip} --batch --forward"
        if check:
            flags += " --dry-run"
        result = await self._executor.execute(
            f"cd /workspace && patch {flags} < {shlex.quote(patch_path)}",
            timeout=120,
            reuse_container=True,
            environment=context.env,
        )
        output = truncate_tail(result.output, prefix="apply_patch")
        if result.success:
            action = "validated" if check else "applied"
            return ToolResult.success(
                f"Patch {action}.\n{output.content or '(no output)'}",
                exit_code=result.exit_code,
                checked=check,
                **output.to_metadata(),
            )
        return ToolResult.error(
            f"Patch failed with exit code {result.exit_code}:\n{output.content}",
            exit_code=result.exit_code,
            checked=check,
            **output.to_metadata(),
        )


class RepoTool(Tool):
    """Self-verifying git/test/project operations for coding agents."""

    _SAFE_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")

    def __init__(self, executor: SandboxExecutor) -> None:
        self._executor = executor

    @property
    def name(self) -> str:
        return "repo"

    @property
    def description(self) -> str:
        return (
            "Inspect and operate on a repo or project in the mounted workspace. "
            "Supports status, diff, branch, test, changed_files, pr_summary, "
            "create_project, init, commit, github_status, and pr_create. "
            "Pass repo_path for non-/workspace projects."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "status",
                        "diff",
                        "branch",
                        "test",
                        "changed_files",
                        "pr_summary",
                        "create_project",
                        "init",
                        "commit",
                        "github_status",
                        "pr_create",
                    ],
                },
                "repo_path": {
                    "type": "string",
                    "description": "Path inside the sandbox. Defaults to /workspace.",
                    "default": "/workspace",
                },
                "project_name": {
                    "type": "string",
                    "description": "Safe folder name for action=create_project.",
                },
                "projects_root": {
                    "type": "string",
                    "description": "Parent directory for action=create_project.",
                    "default": "/workspace/projects",
                },
                "command": {
                    "type": "string",
                    "description": "Test command for action=test.",
                },
                "branch": {
                    "type": "string",
                    "description": "Branch name for action=branch.",
                },
                "message": {
                    "type": "string",
                    "description": "Commit message for action=commit.",
                },
                "title": {
                    "type": "string",
                    "description": "Pull request title for action=pr_create.",
                },
                "body": {
                    "type": "string",
                    "description": "Pull request body for action=pr_create.",
                },
                "base": {
                    "type": "string",
                    "description": "Base ref for diff/pr_summary/pr_create. Default: HEAD for diffs, main for PRs.",
                },
            },
            "required": ["action"],
        }

    def _safe_repo_path(self, value: Any) -> str:
        raw = str(value or "/workspace").strip() or "/workspace"
        path = PurePosixPath(raw if raw.startswith("/") else f"/workspace/{raw}")
        normalized = path.as_posix()
        if normalized == "/":
            return "/workspace"
        if normalized == "/workspace" or normalized.startswith("/workspace/"):
            return normalized
        raise ValueError("repo_path must be inside /workspace")

    def _safe_project_path(self, input_data: dict[str, Any]) -> str | ToolResult:
        name = str(input_data.get("project_name") or "").strip()
        if not name or not self._SAFE_PROJECT_RE.match(name):
            return ToolResult.error(
                "project_name must be 1-80 chars of letters, numbers, dots, underscores, or dashes"
            )
        root = self._safe_repo_path(
            input_data.get("projects_root") or "/workspace/projects"
        )
        return f"{root.rstrip('/')}/{name}"

    async def execute(
        self, input_data: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        action = str(input_data.get("action") or "").strip()
        try:
            repo_path = self._safe_repo_path(input_data.get("repo_path"))
        except ValueError as exc:
            return ToolResult.error(str(exc))

        timeout = 120
        metadata: dict[str, Any] = {"action": action, "repo_path": repo_path}

        if action == "create_project":
            project_path = self._safe_project_path(input_data)
            if isinstance(project_path, ToolResult):
                return project_path
            command = f"mkdir -p {shlex.quote(project_path)} && printf '%s\\n' {shlex.quote(project_path)}"
            repo_path = project_path
            metadata["repo_path"] = repo_path
        elif action == "init":
            command = "git rev-parse --is-inside-work-tree >/dev/null 2>&1 || git init"
        elif action == "status":
            command = "git status --short --branch 2>/dev/null || find . -maxdepth 2 -type f | sort | head -200"
        elif action == "changed_files":
            command = "git diff --name-status HEAD 2>/dev/null; git ls-files --others --exclude-standard 2>/dev/null || true"
        elif action == "diff":
            base = shlex.quote(str(input_data.get("base") or "HEAD"))
            command = (
                "if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then "
                f"git diff --stat {base}; printf '\\n--- DIFF ---\\n'; git diff {base}; "
                "else printf 'Not a git repository. Run repo(action=\"init\") first.\\n'; fi"
            )
        elif action == "branch":
            branch = str(input_data.get("branch") or "").strip()
            if not branch:
                return ToolResult.error("branch is required for action=branch")
            safe = shlex.quote(branch)
            command = f"git switch -c {safe} 2>/dev/null || git switch {safe} && git status --short --branch"
        elif action == "test":
            test_command = str(input_data.get("command") or "").strip()
            if not test_command:
                return ToolResult.error("command is required for action=test")
            command = test_command
            timeout = 600
        elif action == "commit":
            message = str(input_data.get("message") or "").strip()
            if not message:
                return ToolResult.error("message is required for action=commit")
            safe_message = shlex.quote(message)
            command = (
                "git status --short && "
                "git add -A && "
                f"git commit -m {safe_message} && "
                "git status --short --branch"
            )
            timeout = 300
        elif action == "github_status":
            command = (
                "gh auth status && printf '\\n--- REMOTES ---\\n' && git remote -v"
            )
        elif action == "pr_create":
            title = str(input_data.get("title") or "").strip()
            body = str(input_data.get("body") or "").strip()
            base = str(input_data.get("base") or "main").strip()
            if not title:
                return ToolResult.error("title is required for action=pr_create")
            command = (
                "git status --short --branch && "
                "git push -u origin HEAD && "
                "gh pr create "
                f"--base {shlex.quote(base)} "
                f"--title {shlex.quote(title)} "
                f"--body {shlex.quote(body or title)}"
            )
            timeout = 300
        elif action == "pr_summary":
            base = shlex.quote(str(input_data.get("base") or "HEAD"))
            command = (
                "printf 'Branch: '; git branch --show-current; "
                f"printf 'Changed files:\\n'; git diff --name-status {base}; "
                f"printf '\\nDiff stat:\\n'; git diff --stat {base}"
            )
        else:
            return ToolResult.error(
                "Unknown action. Use status, diff, branch, test, changed_files, pr_summary, create_project, init, commit, github_status, or pr_create."
            )

        result = await self._executor.execute(
            f"cd {shlex.quote(repo_path)} && {command}",
            timeout=timeout,
            reuse_container=True,
            environment=context.env,
        )
        output = truncate_tail(result.output, prefix=f"repo_{action}")
        header = [
            f"Repo action: {action}",
            f"Repo path: {repo_path}",
            f"Exit code: {result.exit_code}",
            f"Timed out: {str(result.timed_out).lower()}",
            "Output:",
            output.content or "(no output)",
        ]
        return ToolResult(
            content="\n".join(header),
            is_error=result.timed_out or result.exit_code == -1,
            metadata={
                **metadata,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                **output.to_metadata(),
            },
        )


class CodingJobTool(Tool):
    """Create and inspect persistent coding jobs."""

    def __init__(self, store: CodingJobStore | None = None) -> None:
        self._store = store or CodingJobStore()

    @property
    def name(self) -> str:
        return "coding_job"

    @property
    def description(self) -> str:
        return "Create, list, update, and inspect persistent coding jobs."

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["create", "show", "list", "update"],
                },
                "job_id": {"type": "string"},
                "task": {"type": "string"},
                "repo_path": {"type": "string", "default": "/workspace"},
                "status": {"type": "string"},
                "last_diff_summary": {"type": "string"},
                "last_test_command": {"type": "string"},
                "last_test_result": {"type": "string"},
                "branch": {"type": "string"},
                "project_name": {"type": "string"},
                "last_pr_url": {"type": "string"},
                "last_checkpoint": {"type": "string"},
                "changed_files": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["action"],
        }

    async def execute(
        self, input_data: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        action = str(input_data.get("action") or "").strip()
        if action == "create":
            task = str(input_data.get("task") or "").strip()
            if not task:
                return ToolResult.error("task is required for action=create")
            job = self._store.create(
                task=task,
                repo_path=str(input_data.get("repo_path") or "/workspace"),
                chat_id=context.chat_id,
                user_id=context.user_id,
                provider=context.provider,
                thread_id=context.thread_id,
                telegram_message_id=str(context.metadata.get("message_id") or "")
                or None,
            )
            return ToolResult.success(
                _format_job(job), job_id=job.id, status=job.status
            )
        if action == "list":
            jobs = self._store.list(limit=10)
            body = "\n\n".join(_format_job(job) for job in jobs) or "No coding jobs."
            return ToolResult.success(f"{body}\n\nTotal: {len(jobs)} job(s)")
        job_id = str(input_data.get("job_id") or "").strip()
        job = (
            self._store.get(job_id)
            if job_id
            else self._store.latest_for_chat(
                chat_id=context.chat_id,
                user_id=context.user_id,
                provider=context.provider,
            )
        )
        if job is None:
            return ToolResult.error("Coding job not found")
        if action == "show":
            return ToolResult.success(
                _format_job(job), job_id=job.id, status=job.status
            )
        if action == "update":
            for field in (
                "status",
                "last_diff_summary",
                "last_test_command",
                "last_test_result",
                "branch",
                "project_name",
                "last_pr_url",
                "last_checkpoint",
            ):
                value = input_data.get(field)
                if value is not None:
                    setattr(job, field, str(value))
            changed_files = input_data.get("changed_files")
            if isinstance(changed_files, list):
                job.changed_files = [str(item) for item in changed_files]
            self._store.save(job)
            return ToolResult.success(
                _format_job(job), job_id=job.id, status=job.status
            )
        return ToolResult.error("Unknown action. Use create, show, list, or update.")


def _format_job(job: Any) -> str:
    return "\n".join(
        [
            f"Coding job: {job.id}",
            f"  Status: {job.status}",
            f"  Repo: {job.repo_path}",
            f"  Branch: {job.branch or '(not set)'}",
            f"  Task: {job.task}",
            f"  Updated: {job.updated_at}",
            f"  Project: {job.project_name or '(not set)'}",
            f"  Changed files: {', '.join(job.changed_files) if job.changed_files else '(none)'}",
            f"  Last diff: {job.last_diff_summary or '(none)'}",
            f"  Last test: {job.last_test_result or '(none)'}",
            f"  PR: {job.last_pr_url or '(none)'}",
        ]
    )


class HostedOpenAITool(Tool):
    """Expose an OpenAI hosted/MCP tool definition to compatible LLM providers."""

    def __init__(
        self, name: str, description: str, openai_tool: dict[str, Any]
    ) -> None:
        self._name = name
        self._description = description
        self._openai_tool = openai_tool

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description

    @property
    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    def to_definition(self):  # type: ignore[no-untyped-def]
        from ash.llm.types import ToolDefinition

        return ToolDefinition(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
            kind="hosted",
            metadata={"openai_tool": self._openai_tool},
        )

    async def execute(
        self, input_data: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        return ToolResult.error(
            f"{self.name} is a provider-hosted tool and is not executed by Ash."
        )
