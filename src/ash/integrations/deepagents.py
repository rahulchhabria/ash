"""DeepAgents integration contributor for prompt/runtime discoverability."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ash.deepagents.runtime import LangSmithTraceHelper
from ash.integrations.runtime import IntegrationContext, IntegrationContributor

if TYPE_CHECKING:
    from ash.core.prompt import PromptContext
    from ash.core.session import SessionState


class DeepAgentsIntegration(IntegrationContributor):
    """Advertise optional DeepAgents surfaces in Ash's runtime prompt."""

    name = "deepagents"
    priority = 250

    async def setup(self, context: IntegrationContext) -> None:
        # No dependency import here: deepagents remains optional until a tool/agent
        # actually invokes it.
        return None

    def augment_prompt_context(
        self,
        prompt_context: PromptContext,
        session: SessionState,
        context: IntegrationContext,
    ) -> PromptContext:
        extras = dict(prompt_context.extra_context)
        current = list(extras.get("tool_routing_rules", []))
        current.extend(
            [
                "Use `deep_research` or `use_agent` with agent `deep` for long-horizon work that benefits from planning, delegated subagents, filesystem scratch, and context management.",
                "Use `deepagents_status` to inspect optional DeepAgents/LangSmith setup before relying on DeepAgents-specific behavior.",
                "Use `ash_triage_guidance` when DeepAgents or Ash is diagnosing Linux, Docker, SSH, networking, or service failures.",
            ]
        )
        extras["tool_routing_rules"] = current
        prompt_context.extra_context = extras
        return prompt_context

    def augment_sandbox_env(
        self,
        env: dict[str, str],
        session: SessionState,
        effective_user_id: str,
        context: IntegrationContext,
    ) -> dict[str, str]:
        status = LangSmithTraceHelper().status()
        if status["enabled"]:
            env.setdefault("LANGSMITH_TRACING", "true")
            env.setdefault("LANGSMITH_PROJECT", str(status["project"]))
        return env
