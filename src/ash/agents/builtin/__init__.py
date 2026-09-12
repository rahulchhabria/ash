"""Built-in agents shipped with Ash."""

from ash.agents.builtin.coding import CodingAgent
from ash.agents.builtin.conduit import ConduitAgent
from ash.agents.builtin.deep import DeepAgent
from ash.agents.builtin.research import ResearchAgent
from ash.agents.builtin.task import TaskAgent
from ash.research import ResearchService

__all__ = [
    "CodingAgent",
    "ConduitAgent",
    "DeepAgent",
    "ResearchAgent",
    "TaskAgent",
]


def register_builtin_agents(registry, config=None, tool_executor=None) -> None:
    """Register all built-in agents."""
    registry.register(TaskAgent())
    coding_model = getattr(getattr(config, "coding", None), "model", None)
    if coding_model and coding_model not in getattr(config, "models", {}):
        coding_model = None
    registry.register(CodingAgent(model_alias=coding_model))
    registry.register(ConduitAgent())
    registry.register(DeepAgent(config=config, tool_executor=tool_executor))
    registry.register(ResearchAgent(ResearchService(config=config)))
