"""Passthrough DeepAgents agent for Ash's /deep-style delegation."""

from __future__ import annotations

import os

from ash.agents.base import Agent, AgentConfig, AgentContext, AgentResult
from ash.config.paths import get_workspace_path
from ash.deepagents.runtime import (
    AshDeepAgentsUnavailable,
    DeepAgentsRunner,
    build_workspace_system_prompt,
)


class DeepAgent(Agent):
    """Passthrough agent that runs a LangChain DeepAgents harness."""

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
            or os.environ.get("ASH_DEEPAGENTS_MODEL")
            or "openai:gpt-5.1"
        )
        system_prompt = str(context.input_data.get("system_prompt") or "").strip()
        base = (
            system_prompt
            or "You are Ash's deep mode subagent. Work autonomously on the requested task."
        )
        if context.voice:
            base = (
                f"{base}\n\n## Ash voice for final user-facing prose\n{context.voice}"
            )
        runner = DeepAgentsRunner(
            model=str(requested_model),
            system_prompt=build_workspace_system_prompt(base),
            workspace_path=get_workspace_path(),
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
