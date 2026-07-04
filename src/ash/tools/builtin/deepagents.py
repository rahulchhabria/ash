"""Ash tools backed by optional LangChain DeepAgents integrations."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

from ash.config.paths import get_workspace_path
from ash.deepagents.runtime import (
    AshDeepAgentsUnavailable,
    AshToolCallableFactory,
    DeepAgentsCodeHelper,
    DeepAgentsRunner,
    LangSmithTraceHelper,
    build_workspace_system_prompt,
)
from ash.tools.base import Tool, ToolContext, ToolResult


class DeepResearchTool(Tool):
    """Run a long-horizon DeepAgents research/deep-work loop from Ash."""

    def __init__(self, tool_executor: Any | None = None) -> None:
        self._tool_executor = tool_executor

    @property
    def name(self) -> str:
        return "deep_research"

    @property
    def description(self) -> str:
        return (
            "Delegate a complex long-horizon research/planning task to LangChain "
            "DeepAgents. Use for deep multi-step work where a dedicated harness helps."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Task for the deep agent."},
                "model": {
                    "type": "string",
                    "description": "Optional LangChain model id, e.g. openai:gpt-5.1.",
                },
                "system_prompt": {
                    "type": "string",
                    "description": "Optional additional steering for this run.",
                },
            },
            "required": ["task"],
        }

    async def execute(
        self, input_data: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        task = str(input_data.get("task") or "").strip()
        if not task:
            return ToolResult.error("Missing required parameter: task")

        model = DeepAgentsRunner._normalize_model(
            str(
                input_data.get("model")
                or os.environ.get("ASH_DEEPAGENTS_MODEL")
                or "openai:gpt-5.1"
            )
        )
        extra = str(input_data.get("system_prompt") or "").strip()
        base_prompt = (
            "You are Ash's DeepAgents research/deep-work subagent. "
            "Work autonomously, keep artifacts organized, and report limitations."
        )
        if extra:
            base_prompt = f"{base_prompt}\n\n## Run-specific instructions\n{extra}"

        tools: list[Any] = []
        if self._tool_executor is not None:
            factory = AshToolCallableFactory(self._tool_executor, context)
            for tool_name in (
                "web_search",
                "web_fetch",
                "read_file",
                "write_file",
                "bash",
                "ash_triage_guidance",
            ):
                if tool_name in self._tool_executor.available_tools:
                    tools.append(factory.make_async_callable(tool_name))

        runner = DeepAgentsRunner(
            model=model,
            tools=tools,
            system_prompt=build_workspace_system_prompt(base_prompt),
            workspace_path=get_workspace_path(),
        )
        try:
            result = await runner.ainvoke(task)
        except AshDeepAgentsUnavailable as exc:
            return ToolResult.error(str(exc), dependency="deepagents")
        except Exception as exc:
            return ToolResult.error(f"DeepAgents run failed: {exc}")
        return ToolResult.success(
            result or "(deep agent completed with no text output)"
        )


class DeepAgentsStatusTool(Tool):
    """Report and configure optional DeepAgents/LangSmith surfaces."""

    @property
    def name(self) -> str:
        return "deepagents_status"

    @property
    def description(self) -> str:
        return (
            "Inspect Ash's DeepAgents integration status, LangSmith tracing state, "
            "and Deep Agents Code handoff instructions."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "configure_langsmith": {
                    "type": "boolean",
                    "description": "Set LANGSMITH_TRACING/LANGSMITH_PROJECT in this process.",
                    "default": False,
                }
            },
        }

    async def execute(
        self, input_data: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        if input_data.get("configure_langsmith"):
            LangSmithTraceHelper().configure_environment()
        payload = {
            "deepagents_installed": importlib.util.find_spec("deepagents") is not None,
            "langsmith": LangSmithTraceHelper().status(),
            "ash_workspace": str(get_workspace_path()),
            "deep_agents_code": DeepAgentsCodeHelper().instructions(),
        }
        return ToolResult.success(json.dumps(payload, indent=2))


class AshTriageDeepAgentsTool(Tool):
    """Expose ash-triage-api guidance to Ash/DeepAgents workflows."""

    @property
    def name(self) -> str:
        return "ash_triage_guidance"

    @property
    def description(self) -> str:
        return (
            "Get Pioneer/ash-triage diagnostic guidance for Linux, Docker, SSH, "
            "networking, or service failure context."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "error_context": {
                    "type": "string",
                    "description": "Logs, command output, or symptoms to diagnose.",
                },
                "api_key": {
                    "type": "string",
                    "description": "Optional Pioneer API key; defaults to PIONEER_API_KEY.",
                },
            },
            "required": ["error_context"],
        }

    async def execute(
        self, input_data: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        error_context = str(input_data.get("error_context") or "").strip()
        if not error_context:
            return ToolResult.error("Missing required parameter: error_context")

        try:
            guidance = await asyncio.to_thread(
                self._get_guidance,
                error_context,
                input_data.get("api_key") or os.environ.get("PIONEER_API_KEY"),
            )
        except Exception as exc:
            return ToolResult.error(f"ash-triage guidance failed: {exc}")
        return ToolResult.success(json.dumps(guidance, indent=2, default=str))

    def _get_guidance(self, error_context: str, api_key: str | None) -> Any:
        # Prefer an installed/importable ash_triage. Fall back to the sibling dev checkout
        # that exists in Rahul's environment without making Ash depend on it.
        if importlib.util.find_spec("ash_triage") is None:
            candidate = Path.home() / "ash-triage-api"
            if candidate.exists():
                sys.path.insert(0, str(candidate))
        from ash_triage import get_triage_guidance  # type: ignore[import-not-found]

        return get_triage_guidance(error_context, api_key)
