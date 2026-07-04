"""Built-in agents shipped with Ash."""

from ash.agents.builtin.deep import DeepAgent
from ash.agents.builtin.research import ResearchAgent
from ash.agents.builtin.task import TaskAgent
from ash.research import ResearchService

__all__ = [
    "DeepAgent",
    "ResearchAgent",
    "TaskAgent",
]


def register_builtin_agents(registry, config=None) -> None:
    """Register all built-in agents."""
    registry.register(TaskAgent())
    registry.register(DeepAgent())
    registry.register(ResearchAgent(ResearchService(config=config)))
