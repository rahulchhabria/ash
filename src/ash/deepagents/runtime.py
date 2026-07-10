"""Adapters between Ash and LangChain DeepAgents.

The integration is intentionally optional. Ash should remain installable and usable
without ``deepagents`` or LangSmith. Objects in this module either dynamically import
DeepAgents at call time or expose small backend/bridge classes that can be supplied to
DeepAgents once the dependency is installed.
"""

from __future__ import annotations

import asyncio
import fnmatch
import inspect
import json
import os
import shlex
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ash.config.paths import get_workspace_path

MAX_WORKSPACE_SEARCH_FILES = 10_000
MAX_WORKSPACE_SEARCH_FILE_BYTES = 1_000_000

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

        return asyncio.run(self.als(path))

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> Any:
        import asyncio

        return asyncio.run(self.aread(file_path, offset, limit))

    def write(self, file_path: str, content: str) -> Any:
        import asyncio

        return asyncio.run(self.awrite(file_path, content))

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> Any:
        import asyncio

        return asyncio.run(self.aedit(file_path, old_string, new_string, replace_all))

    def glob(self, pattern: str, path: str | None = None) -> Any:
        import asyncio

        return asyncio.run(self.aglob(pattern, path))

    def grep(
        self, pattern: str, path: str | None = None, glob: str | None = None
    ) -> Any:
        import asyncio

        return asyncio.run(self.agrep(pattern, path, glob))

    # --- async surface ---
    async def als(self, path: str) -> Any:
        result_types = _deepagents_result_types()
        try:
            entries = await self._backend.list_file_infos(path, recursive=False)
        except Exception as exc:
            return result_types["LsResult"](error=str(exc), entries=None)
        return result_types["LsResult"](entries=entries)

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> Any:
        result_types = _deepagents_result_types()
        try:
            content = await self._backend.read_file(file_path)
        except Exception as exc:
            return result_types["ReadResult"](error=str(exc))
        lines = content.splitlines(keepends=True)
        if offset or limit:
            lines = lines[offset : offset + limit] if limit else lines[offset:]
        file_data = {"content": "".join(lines), "encoding": "utf-8"}
        return result_types["ReadResult"](file_data=file_data)

    async def awrite(self, file_path: str, content: str) -> Any:
        result_types = _deepagents_result_types()
        try:
            written_path = await self._backend.write_file(
                file_path, content, overwrite=False
            )
        except Exception as exc:
            return result_types["WriteResult"](error=str(exc))
        return result_types["WriteResult"](path=f"/{written_path}")

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> Any:
        result_types = _deepagents_result_types()
        try:
            content = await self._backend.read_file(file_path)
            occurrences = content.count(old_string)
            if occurrences == 0:
                return result_types["EditResult"](
                    error=f"String to replace was not found in {file_path}"
                )
            if not replace_all and occurrences > 1:
                return result_types["EditResult"](
                    error=(
                        f"String to replace appears {occurrences} times in "
                        f"{file_path}; pass replace_all=True to replace all occurrences"
                    )
                )
            new_content = content.replace(
                old_string, new_string, occurrences if replace_all else 1
            )
            written_path = await self._backend.write_file(
                file_path, new_content, overwrite=True
            )
        except Exception as exc:
            return result_types["EditResult"](error=str(exc))
        return result_types["EditResult"](
            path=f"/{written_path}",
            occurrences=occurrences if replace_all else 1,
        )

    async def aglob(self, pattern: str, path: str | None = None) -> Any:
        result_types = _deepagents_result_types()
        try:
            matches = await self._backend.glob_file_infos(pattern, path or ".")
        except Exception as exc:
            return result_types["GlobResult"](error=str(exc), matches=[])
        return result_types["GlobResult"](matches=matches)

    async def agrep(
        self, pattern: str, path: str | None = None, glob: str | None = None
    ) -> Any:
        result_types = _deepagents_result_types()
        try:
            matches = await self._backend.search_matches(pattern, path or ".", glob)
        except Exception as exc:
            return result_types["GrepResult"](error=str(exc), matches=[])
        return result_types["GrepResult"](matches=matches)

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


def _deepagents_result_types() -> dict[str, Any]:
    from deepagents.backends.protocol import (
        EditResult,
        GlobResult,
        GrepResult,
        LsResult,
        ReadResult,
        WriteResult,
    )

    return {
        "EditResult": EditResult,
        "GlobResult": GlobResult,
        "GrepResult": GrepResult,
        "LsResult": LsResult,
        "ReadResult": ReadResult,
        "WriteResult": WriteResult,
    }


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


async def _call_with_supported_kwargs(
    func: Callable[..., Any], *args: Any, **kwargs: Any
) -> Any:
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        supported_kwargs: dict[str, Any] = {}
    else:
        parameters = signature.parameters.values()
        accepts_var_kwargs = any(
            param.kind is inspect.Parameter.VAR_KEYWORD for param in parameters
        )
        if accepts_var_kwargs:
            supported_kwargs = kwargs
        else:
            accepted_names = {
                param.name
                for param in parameters
                if param.kind
                in {
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.KEYWORD_ONLY,
                }
            }
            supported_kwargs = {
                key: value for key, value in kwargs.items() if key in accepted_names
            }
    return await _call_maybe_async(func, *args, **supported_kwargs)


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
        candidate = self._normalize_virtual_path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.expanduser().resolve()
        root = self.root.expanduser().resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError(f"Path escapes Ash workspace: {path}")
        return resolved

    @staticmethod
    def _normalize_virtual_path(path: str | Path) -> Path:
        text = str(path).strip() or "."
        if text.startswith("~"):
            raise ValueError(f"Path escapes Ash workspace: {path}")
        candidate = Path(text)
        if candidate.is_absolute():
            # DeepAgents filesystem paths are slash-rooted virtual paths.
            candidate = Path(text.lstrip("/") or ".")
        if ".." in candidate.parts:
            raise ValueError(f"Path escapes Ash workspace: {path}")
        return candidate

    def _workspace_root(self) -> Path:
        return self.root.expanduser().resolve()

    def _relative_path(self, path: Path) -> str:
        return path.relative_to(self._workspace_root()).as_posix()

    def _file_info(self, path: Path) -> dict[str, Any]:
        rel = self._relative_path(path)
        info: dict[str, Any] = {
            "path": f"/{rel}",
            "is_dir": path.is_dir(),
        }
        try:
            stat = path.stat()
            info["size"] = int(stat.st_size)
            info["modified_at"] = str(stat.st_mtime)
        except OSError:
            pass
        if info["is_dir"] and not info["path"].endswith("/"):
            info["path"] += "/"
        return info

    async def read_file(self, path: str) -> str:
        return await asyncio.to_thread(lambda: self._resolve(path).read_text())

    async def write_file(
        self, path: str, content: str, *, overwrite: bool = True
    ) -> str:
        def _write() -> str:
            resolved = self._resolve(path)
            if not overwrite and resolved.exists():
                raise FileExistsError(f"File already exists: {path}")
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(content)
            return self._relative_path(resolved)

        return await asyncio.to_thread(_write)

    async def list_files(self, path: str = ".") -> list[str]:
        def _list() -> list[str]:
            resolved = self._resolve(path)
            files = sorted(p for p in resolved.rglob("*") if p.is_file())
            return [self._relative_path(p) for p in files]

        return await asyncio.to_thread(_list)

    async def list_file_infos(
        self, path: str = ".", *, recursive: bool = True
    ) -> list[dict[str, Any]]:
        def _list() -> list[dict[str, Any]]:
            resolved = self._resolve(path)
            iterator = resolved.rglob("*") if recursive else resolved.iterdir()
            return sorted(
                (self._file_info(p) for p in iterator),
                key=lambda item: str(item.get("path", "")),
            )

        return await asyncio.to_thread(_list)

    async def glob_file_infos(
        self, pattern: str, path: str = "."
    ) -> list[dict[str, Any]]:
        def _glob() -> list[dict[str, Any]]:
            resolved = self._resolve(path)
            normalized_pattern = pattern.lstrip("/") or "*"
            matches: list[dict[str, Any]] = []
            for item in resolved.rglob("*"):
                if not item.is_file():
                    continue
                rel = self._relative_path(item)
                if fnmatch.fnmatch(rel, normalized_pattern) or fnmatch.fnmatch(
                    item.name, normalized_pattern
                ):
                    matches.append(self._file_info(item))
            return sorted(matches, key=lambda entry: str(entry.get("path", "")))

        return await asyncio.to_thread(_glob)

    async def search(self, query: str, path: str = ".") -> list[str]:
        matches = await self.search_matches(query, path)
        return sorted({str(match["path"]).lstrip("/") for match in matches})

    async def search_matches(
        self, query: str, path: str = ".", glob: str | None = None
    ) -> list[dict[str, Any]]:
        resolved = self._resolve(path)

        def _scan() -> list[dict[str, Any]]:
            matches: list[dict[str, Any]] = []
            scanned = 0
            for item in resolved.rglob("*"):
                if not item.is_file():
                    continue
                scanned += 1
                if scanned > MAX_WORKSPACE_SEARCH_FILES:
                    break
                rel = self._relative_path(item)
                if glob and not (
                    fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(item.name, glob)
                ):
                    continue
                try:
                    if item.stat().st_size > MAX_WORKSPACE_SEARCH_FILE_BYTES:
                        continue
                    lines = item.read_text(errors="ignore").splitlines()
                except OSError:
                    continue
                for line_number, line in enumerate(lines, start=1):
                    if query in line:
                        matches.append(
                            {
                                "path": f"/{rel}",
                                "line": line_number,
                                "text": line,
                            }
                        )
            return matches

        return await asyncio.to_thread(_scan)


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
        result = await _call_with_supported_kwargs(search, query, limit=limit)
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
            result = await _call_with_supported_kwargs(method, content, **metadata)
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
            "  1. Open https://docs.langchain.com/deep-agents in a browser.\n"
            "  2. Follow the documented Deep Agents Code installation steps for your platform.\n"
            "  3. Verify downloaded installers or scripts before executing them; do not pipe\n"
            "     remote scripts directly into a shell without explicit confirmation.\n\n"
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
    if kind == "ssh" and target.strip().startswith("-"):
        raise ValueError("SSH target must not start with '-'")
    safe_target = shlex.quote(target)
    if kind == "docker":
        return f"docker ps -a | grep {safe_target}; docker logs {safe_target} 2>&1 | tail -50"
    if kind == "ssh":
        return f"ssh -v {safe_target} 2>&1"
    return safe_target
