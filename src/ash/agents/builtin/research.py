"""DeepAgents-backed research agent."""

from __future__ import annotations

from ash.agents.base import Agent, AgentConfig, AgentContext, AgentResult
from ash.research import ResearchRequest, ResearchService


class ResearchAgent(Agent):
    """Passthrough agent that runs the research pipeline."""

    def __init__(self, service: ResearchService) -> None:
        self._service = service

    @property
    def config(self) -> AgentConfig:
        return AgentConfig(
            name="research",
            description=(
                "Run an asynchronous research pipeline using DeepAgents for collection, "
                "GLiNER for extraction, and optional Codex review."
            ),
            system_prompt="Passthrough DeepAgents research agent.",
            is_passthrough=True,
            enable_progress_updates=False,
            timeout=1200,
        )

    async def execute_passthrough(
        self,
        message: str,
        context: AgentContext,
        model: str | None = None,
    ) -> AgentResult:
        request = ResearchRequest.from_input(message, context.input_data)
        result = await self._service.run(request)
        if result.status == "failed":
            return AgentResult.error(result.to_user_message())
        return AgentResult.success(
            result.to_user_message(),
            metadata={
                "document_path": str(result.report_path),
                "document_caption": "Research report attached.",
                "report_path": str(result.report_path),
                "brief_path": str(result.brief_path),
                "facts_path": str(result.facts_path),
                "sources_path": str(result.sources_path),
                "actions_path": str(result.actions_path),
                "job_dir": str(result.job_dir),
            },
        )
