"""Adapters between Ash and LangChain DeepAgents.

The integration is intentionally optional. Ash should remain installable and usable
without ``deepagents`` or LangSmith. Objects in this module either dynamically import
DeepAgents at call time or expose small backend/bridge classes that can be supplied to
DeepAgents once the dependency is installed.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import shlex
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ash.config.paths import get_workspace_path

if TYPE_CHECKING:
    from ash.sandbox import SandboxExecutor
    from ash.skills.registry import SkillRegistry
    from ash.store.store import Store
    from ash.tools.base import ToolContext
    from ash.tools.executor import ToolExecutor


class _AshToDeepAgentBackendAdapter:
    """Thin adapter so AshFilesystemBackend satisfies deepagents BackendProtocol."""

    def __init__(self, root: Path | None = None):
        self._backend = AshFilesystemBackend(root=root or get_workspace_path())
        self._root = self._backend.root

    # --- sync surface (BackendProtocol requires sync + async) ---
    def ls(self, path: str) -> Any:
        import asyncio

        return asyncio.run(self._backend.list_files(path))

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> Any:
        import asyncio

        return asyncio.run(self._async_read(file_path, offset, limit))

    def write(self, file_path: str, content: str) -> Any:
        import asyncio

        return asyncio.run(self._async_write(file_path, content))

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> Any:
        import asyncio

        return asyncio.run(
            self._async_edit(file_path, old_string, new_string, replace_all)
        )

    def glob(self, pattern: str, path: str | None = None) -> Any:
        import asyncio

        return asyncio.run(self._async_list_files(path or "."))

    def grep(
        self, pattern: str, path: str | None = None, glob: str | None = None
    ) -> Any:
        import asyncio

        return asyncio.run(self._backend.search(pattern, path or "."))

    # --- async surface ---
    async def als(self, path: str) -> Any:
        return await self._backend.list_files(path)

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> Any:
        return await self._async_read(file_path, offset, limit)

    async def awrite(self, file_path: str, content: str) -> Any:
        return await self._async_write(file_path, content)

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> Any:
        return await self._async_edit(file_path, old_string, new_string, replace_all)

    async def aglob(self, pattern: str, path: str | None = None) -> Any:
        return await self._backend.list_files(path or ".")

    async def agrep(
        self, pattern: str, path: str | None = None, glob: str | None = None
    ) -> Any:
        return await self._backend.search(pattern, path or ".")

    # --- helpers ---
    async def _async_read(
        self, file_path: str, offset: int = 0, limit: int = 2000
    ) -> str:
        content = await self._backend.read_file(file_path)
        lines = content.split("\n")
        if offset or limit:
            lines = lines[offset : offset + limit] if limit else lines[offset:]
        return "\n".join(lines)

    async def _async_write(self, file_path: str, content: str) -> str:
        return await self._backend.write_file(file_path, content)

    async def _async_edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> str:
        content = await self._backend.read_file(file_path)
        if replace_all:
            content = content.replace(old_string, new_string)
        else:
            content = content.replace(old_string, new_string, 1)
        await self._backend.write_file(file_path, content)
        return str(self._backend._resolve(file_path).relative_to(self._root))

    async def _async_list_files(self, path: str) -> list[str]:
        return await self._backend.list_files(path)

    # --- stub the rest ---
    def ls_info(self, path: str) -> Any:
        return []

    async def als_info(self, path: str) -> Any:
        return []

    def glob_info(self, pattern: str, path: str = "/") -> Any:
        return []

    async def aglob_info(self, pattern: str, path: str = "/") -> Any:
        return []

    def grep_raw(
        self, pattern: str, path: str | None = None, glob: str | None = None
    ) -> Any:
        return []

    async def agrep_raw(
        self, pattern: str, path: str | None = None, glob: str | None = None
    ) -> Any:
        return []

    def download_files(self, paths: list[str]) -> Any:
        return []

    async def adownload_files(self, paths: list[str]) -> Any:
        return []

    def upload_files(self, files: list[tuple[str, bytes]]) -> Any:
        return []

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> Any:
        return []


def _default_tool_context() -> Any:
    from ash.tools.base import ToolContext

    return ToolContext()


class AshDeepAgentsUnavailable(RuntimeError):
    """Raised when an optional DeepAgents surface is invoked without dependency."""


def require_deepagents() -> Callable[..., Any]:
    """Import and return ``deepagents.create_deep_agent`` with a helpful error."""
    try:
        from deepagents import create_deep_agent
    except ImportError as exc:  # pragma: no cover - dependency optional
        raise AshDeepAgentsUnavailable(
            "The optional 'deepagents' package is not installed. Install it with "
            "`uv sync --extra deepagents` or `uv add deepagents`."
        ) from exc
    return create_deep_agent


def _extract_text(value: Any) -> str:
    """Best-effort extraction of model output from DeepAgents/LangGraph results."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        messages = value.get("messages")
        if isinstance(messages, list) and messages:
            last = messages[-1]
            content = getattr(last, "content", None)
            if content is not None:
                return _extract_text(content)
            if isinstance(last, dict) and "content" in last:
                return _extract_text(last["content"])
        for key in ("output", "result", "content", "final", "response"):
            if key in value:
                return _extract_text(value[key])
        return json.dumps(value, indent=2, default=str)
    if isinstance(value, list):
        return "\n".join(_extract_text(item) for item in value)
    content = getattr(value, "content", None)
    if content is not None:
        return _extract_text(content)
    return str(value)


def _call_maybe_async(
    func: Callable[..., Any], *args: Any, **kwargs: Any
) -> Awaitable[Any]:
    result = func(*args, **kwargs)
    if inspect.isawaitable(result):
        return result

    async def _wrapped() -> Any:
        return result

    return _wrapped()


@dataclass(slots=True)
class DeepAgentsRunner:
    """Small facade for invoking DeepAgents from Ash tools/agents.

    This powers integration ideas #1 and #4: an Ash tool/agent delegates a hard,
    long-horizon task to DeepAgents while Ash continues to own personality,
    sessions, memory, Telegram, and tool policy.
    """

    model: str = "openai:gpt-5.1"
    system_prompt: str = "You are a focused deep work subagent for Ash."
    tools: list[Any] = field(default_factory=list)
    workspace_path: Path | None = None
    extra_kwargs: dict[str, Any] = field(default_factory=dict)

    def create(self) -> Any:
        create_deep_agent = require_deepagents()
        kwargs = dict(self.extra_kwargs)
        # deepagents 0.6.x uses `backend=` for filesystem, not `workspace_path=`.
        # Wire the AshFilesystemBackend as a DeepAgents BackendProtocol adapter
        # if no backend was provided and a workspace_path is configured.
        if kwargs.get("backend") is None and self.workspace_path is not None:
            kwargs["backend"] = _AshToDeepAgentBackendAdapter(root=self.workspace_path)
        # Normalize the model string: langchain needs a known provider prefix
        # (e.g. "openai:gpt-5.6") to infer the provider.  Bare model IDs won't
        # work.  If no provider prefix is detected, wrap with "openai:".
        model = self._normalize_model(self.model)
        # Explicit model_provider prevents langchain from guessing wrong.
        return create_deep_agent(
            model=model,
            tools=self.tools,
            system_prompt=self.system_prompt,
            **kwargs,
        )

    @staticmethod
    def _normalize_model(model: str) -> str:
        """Ensure model has a langchain-compatible provider prefix."""
        if ":" in model:
            # Already prefixed, e.g. "openai:gpt-5.6" — pass through
            return model
        # Bare model ID — wrap with "openai:" for the OpenAI provider.
        return f"openai:{model}"

    async def ainvoke(self, message: str) -> str:
        agent = self.create()
        payload = {"messages": message}
        if hasattr(agent, "ainvoke"):
            result = await agent.ainvoke(payload)
        else:
            result = await asyncio.to_thread(agent.invoke, payload)
        return _extract_text(result)


@dataclass(slots=True)
class AshSandboxShellBackend:
    """DeepAgents shell backend backed by Ash's Docker sandbox (#2)."""

    executor: SandboxExecutor
    context: ToolContext = field(default_factory=_default_tool_context)

    async def run(self, command: str, command_timeout: int = 60) -> dict[str, Any]:
        result = await self.executor.execute(
            command,
            timeout=command_timeout,
            reuse_container=True,
            environment=self.context.env,
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "output": result.output,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "success": result.success,
        }

    async def __call__(self, command: str, command_timeout: int = 60) -> str:
        result = await self.run(command, command_timeout=command_timeout)
        prefix = f"exit_code={result['exit_code']}"
        return f"{prefix}\n{result['output']}"


@dataclass(slots=True)
class AshFilesystemBackend:
    """Filesystem backend constrained to Ash's workspace (#6).

    Methods are deliberately simple so they can be passed to DeepAgents versions
    that accept pluggable filesystem callables, or used by Ash's own wrappers.
    """

    root: Path = field(default_factory=get_workspace_path)

    def _resolve(self, path: str | Path) -> Path:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.expanduser().resolve()
        root = self.root.expanduser().resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError(f"Path escapes Ash workspace: {path}")
        return resolved

    async def read_file(self, path: str) -> str:
        return self._resolve(path).read_text()

    async def write_file(self, path: str, content: str) -> str:
        resolved = self._resolve(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content)
        return str(resolved.relative_to(self.root.expanduser().resolve()))

    async def list_files(self, path: str = ".") -> list[str]:
        resolved = self._resolve(path)
        files = sorted(p for p in resolved.rglob("*") if p.is_file())
        root = self.root.expanduser().resolve()
        return [str(p.relative_to(root)) for p in files]

    async def search(self, query: str, path: str = ".") -> list[str]:
        resolved = self._resolve(path)

        def _scan() -> list[str]:
            matches: list[str] = []
            for item in resolved.rglob("*"):
                if not item.is_file():
                    continue
                try:
                    text = item.read_text(errors="ignore")
                except OSError:
                    continue
                if query in text:
                    matches.append(
                        str(item.relative_to(self.root.expanduser().resolve()))
                    )
            return matches

        return _scan()


@dataclass(slots=True)
class AshSkillBridge:
    """Bridge Ash SKILL.md definitions into DeepAgents skill descriptors (#3)."""

    registry: SkillRegistry

    def list_skill_descriptors(self) -> list[dict[str, Any]]:
        descriptors: list[dict[str, Any]] = []
        for skill in self.registry:
            descriptors.append(
                {
                    "name": skill.name,
                    "description": skill.description,
                    "instructions": skill.instructions,
                    "path": str(skill.skill_path) if skill.skill_path else None,
                    "source_type": skill.source_type.value,
                    "allowed_tools": list(skill.allowed_tools),
                    "metadata": dict(skill.metadata),
                }
            )
        return descriptors

    def render_for_prompt(self, names: Iterable[str] | None = None) -> str:
        wanted = set(names or [])
        sections: list[str] = []
        for item in self.list_skill_descriptors():
            if wanted and item["name"] not in wanted:
                continue
            sections.append(
                f"## {item['name']}\n\n{item['description']}\n\n{item['instructions']}"
            )
        return "\n\n".join(sections)


@dataclass(slots=True)
class AshToolCallableFactory:
    """Expose selected Ash tools as DeepAgents/LangChain-compatible callables."""

    executor: ToolExecutor
    context: ToolContext = field(default_factory=_default_tool_context)

    def make_async_callable(self, tool_name: str) -> Callable[..., Awaitable[str]]:
        async def _async_tool(**kwargs: Any) -> str:
            result = await self.executor.execute(tool_name, kwargs, self.context)
            if result.is_error:
                return f"ERROR: {result.content}"
            return result.content

        _async_tool.__name__ = f"ash_{tool_name}"
        _async_tool.__doc__ = f"Invoke Ash tool '{tool_name}' with keyword arguments."
        return _async_tool

    def make_callable(self, tool_name: str) -> Callable[..., Any]:
        async_tool = self.make_async_callable(tool_name)

        def _tool(**kwargs: Any) -> str:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                return asyncio.run(async_tool(**kwargs))
            raise RuntimeError(
                f"Ash tool callable '{tool_name}' was called synchronously inside an active event loop. "
                "Use make_async_callable() for async agent runtimes."
            ) from None

        _tool.__name__ = f"ash_{tool_name}"
        _tool.__doc__ = f"Invoke Ash tool '{tool_name}' with keyword arguments."
        return _tool


@dataclass(slots=True)
class AshMemoryStoreAdapter:
    """Persistent memory adapter backed by Ash's store (#8)."""

    store: Store | None

    async def search(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        if self.store is None:
            return []
        search = getattr(self.store, "search", None) or getattr(
            self.store, "search_memories", None
        )
        if search is None:
            return []
        try:
            result = await _call_maybe_async(search, query, limit=limit)
        except TypeError:
            result = await _call_maybe_async(search, query)
        if result is None:
            return []
        if isinstance(result, list):
            return [
                item if isinstance(item, dict) else {"value": str(item)}
                for item in result
            ]
        return [{"value": str(result)}]

    async def add(self, content: str, **metadata: Any) -> dict[str, Any]:
        if self.store is None:
            return {"stored": False, "reason": "ash memory is disabled"}
        for method_name in ("add_memory", "add", "create_memory"):
            method = getattr(self.store, method_name, None)
            if method is None:
                continue
            try:
                result = await _call_maybe_async(method, content, **metadata)
            except TypeError:
                result = await _call_maybe_async(method, content)
            return {"stored": True, "result": str(result)}
        return {"stored": False, "reason": "store has no supported add method"}


@dataclass(slots=True)
class LangSmithTraceHelper:
    """LangSmith tracing setup helper for Ash/DeepAgents runs (#7)."""

    project: str = "ash-deepagents"

    def configure_environment(self) -> dict[str, str]:
        env = {
            "LANGSMITH_TRACING": os.environ.get("LANGSMITH_TRACING", "true"),
            "LANGSMITH_PROJECT": os.environ.get("LANGSMITH_PROJECT", self.project),
        }
        os.environ.update(env)
        return env

    def status(self) -> dict[str, Any]:
        return {
            "enabled": os.environ.get("LANGSMITH_TRACING") in {"true", "1", "yes"},
            "project": os.environ.get("LANGSMITH_PROJECT", self.project),
            "api_key_configured": bool(os.environ.get("LANGSMITH_API_KEY")),
        }


@dataclass(slots=True)
class TelegramHITLApprover:
    """Human-in-the-loop approval hook designed for Telegram routing (#9)."""

    request_approval: Callable[[dict[str, Any]], Awaitable[bool]] | None = None
    auto_approve_readonly: bool = True

    async def approve_tool_call(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> bool:
        if self.auto_approve_readonly and tool_name in {
            "read_file",
            "web_search",
            "web_fetch",
        }:
            return True
        if self.request_approval is None:
            return False
        return bool(
            await self.request_approval({"tool": tool_name, "arguments": arguments})
        )


@dataclass(slots=True)
class DeepAgentsCodeHelper:
    """Instructions for installing/running Deep Agents Code beside Ash (#10)."""

    workspace: Path = field(default_factory=get_workspace_path)

    def instructions(self) -> str:
        return (
            "Deep Agents Code can run alongside Ash for terminal coding tasks.\n\n"
            "Install:\n"
            "  curl -LsSf https://langch.in/dcode | bash\n\n"
            "Suggested handoff workspace:\n"
            f"  {self.workspace}\n\n"
            "Keep Ash as the memory/personality/Telegram orchestrator and use Deep Agents "
            "Code for focused repository edits. Store handoff briefs and artifacts under "
            "the Ash workspace so both tools can see them."
        )


def build_workspace_system_prompt(base: str, workspace: Path | None = None) -> str:
    workspace = workspace or get_workspace_path()
    return (
        f"{base}\n\n"
        "## Ash Integration Contract\n"
        f"- Treat {workspace} as the shared filesystem of record.\n"
        "- Prefer Ash-provided tools/backends for shell, files, memory, and approvals.\n"
        "- Return a concise final answer plus paths to any artifacts you created."
    )


def shell_command_for_triage(kind: str, target: str) -> str:
    """Build a conservative diagnostic command for ash-triage handoff (#5)."""
    safe_target = shlex.quote(target)
    if kind == "docker":
        return f"docker ps -a | grep {safe_target}; docker logs {safe_target} 2>&1 | tail -50"
    if kind == "ssh":
        return f"ssh -v {safe_target} 2>&1"
    return safe_target
