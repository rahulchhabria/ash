"""Passthrough DeepAgents agent for Ash's /deep-style delegation."""

from __future__ import annotations

import os

from ash.agents.base import Agent, AgentConfig, AgentContext, AgentResult
from ash.config.paths import get_workspace_path
from ash.deepagents.runtime import (
    AshDeepAgentsUnavailable,
    DeepAgentsRunner,
    build_deepagents_toolset,
    build_workspace_system_prompt,
)
from ash.tools.base import ToolContext
from ash.tools.trust import ToolOutputTrustPolicy


class DeepAgent(Agent):
    """Passthrough agent that runs a LangChain DeepAgents harness."""

    def __init__(self, config: object | None = None, tool_executor=None) -> None:
        self._ash_config = config
        self._tool_executor = tool_executor

    @property
    def config(self) -> AgentConfig:
        return AgentConfig(
            name="deep",
            description=(
                "Run a LangChain DeepAgents harness for long-horizon multi-step work. "
                "Use this when planning, context management, or delegation matters."
            ),
            system_prompt="Passthrough LangChain DeepAgents agent.",
            is_passthrough=True,
            enable_progress_updates=False,
            timeout=1800,
        )

    async def execute_passthrough(
        self,
        message: str,
        context: AgentContext,
        model: str | None = None,
    ) -> AgentResult:
        requested_model = (
            model
            or context.input_data.get("model")
            or getattr(getattr(self._ash_config, "deepagents", None), "model", None)
            or os.environ.get("ASH_DEEPAGENTS_MODEL")
            or "openai:gpt-5.1"
        )
        deep_config = getattr(self._ash_config, "deepagents", None)
        if deep_config is not None and not deep_config.enabled:
            return AgentResult.error("DeepAgents orchestration is disabled in config")
        if deep_config is not None:
            message = message[: deep_config.max_task_chars]
        system_prompt = str(context.input_data.get("system_prompt") or "").strip()
        base = (
            system_prompt
            or "You are Pigeon's deep mode subagent. Work autonomously on the requested task."
        )
        if context.voice:
            base = f"{base}\n\n## Pigeon voice for final user-facing prose\n{context.voice}"
        allowed_tools = list(
            getattr(
                deep_config,
                "allowed_tools",
                [
                    "openai_web_search",
                    "web_search",
                    "exa_search",
                    "google_places",
                    "web_fetch",
                    "read_file",
                    "ash_triage_guidance",
                ],
            )
        )
        tool_context = ToolContext.from_agent_context(context)
        tools = build_deepagents_toolset(
            executor=self._tool_executor,
            context=tool_context,
            allowed_tools=allowed_tools,
            model=str(requested_model),
            trust_policy=ToolOutputTrustPolicy.from_config(
                getattr(self._ash_config, "tool_output_trust", object())
            ),
        )
        runner = DeepAgentsRunner(
            model=str(requested_model),
            tools=tools,
            system_prompt=build_workspace_system_prompt(base),
            workspace_path=get_workspace_path(),
            filesystem_mode=getattr(deep_config, "filesystem_mode", "read_only"),
            builtin_subagents=getattr(deep_config, "builtin_subagents", True),
        )
        try:
            result = await runner.ainvoke(message)
        except AshDeepAgentsUnavailable as exc:
            return AgentResult.error(str(exc))
        except Exception as exc:
            return AgentResult.error(f"DeepAgents execution failed: {exc}")
        return AgentResult.success(
            result or "(deep agent completed with no text output)"
        )
