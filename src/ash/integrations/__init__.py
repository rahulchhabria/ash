"""Integration hooks runtime.

Spec contract: specs/subsystems.md (Integration Hooks).
"""

from ash.integrations.browser import BrowserIntegration
from ash.integrations.capabilities import CapabilitiesIntegration
from ash.integrations.close_game_alert import CloseGameAlertIntegration
from ash.integrations.deepagents import DeepAgentsIntegration
from ash.integrations.composer import active_integrations, compose_integrations
from ash.integrations.defaults import DefaultIntegrations, create_default_integrations
from ash.integrations.email_forward_summary import EmailForwardSummaryIntegration
from ash.integrations.image import ImageIntegration
from ash.integrations.memory import MemoryIntegration
from ash.integrations.rpc import active_rpc_server
from ash.integrations.runtime import (
    IntegrationContext,
    IntegrationContributor,
    IntegrationHealthSnapshot,
    IntegrationRuntime,
)
from ash.integrations.runtime_rpc import RuntimeRPCIntegration
from ash.integrations.scheduling import SchedulingIntegration
from ash.integrations.todo import TodoIntegration

__all__ = [
    "MemoryIntegration",
    "BrowserIntegration",
    "CapabilitiesIntegration",
    "CloseGameAlertIntegration",
    "DeepAgentsIntegration",
    "EmailForwardSummaryIntegration",
    "ImageIntegration",
    "TodoIntegration",
    "RuntimeRPCIntegration",
    "SchedulingIntegration",
    "compose_integrations",
    "active_integrations",
    "DefaultIntegrations",
    "create_default_integrations",
    "active_rpc_server",
    "IntegrationContext",
    "IntegrationContributor",
    "IntegrationHealthSnapshot",
    "IntegrationRuntime",
]
