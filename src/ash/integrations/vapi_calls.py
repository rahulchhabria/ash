"""Vapi call tools and restart-safe summary delivery lifecycle.

Spec contract: specs/subsystems.md (Integration Hooks), specs/integrations.md.
"""

from __future__ import annotations

from ash.integrations.runtime import IntegrationContext, IntegrationContributor
from ash.tools.builtin import (
    VapiCallStatusTool,
    VapiEndCallTool,
    VapiOutboundCallTool,
)


class VapiCallsIntegration(IntegrationContributor):
    """Own Vapi tool registration and pending-summary recovery."""

    name = "vapi_calls"
    priority = 275

    def __init__(self) -> None:
        self._outbound_tool: VapiOutboundCallTool | None = None

    @property
    def outbound_tool(self) -> VapiOutboundCallTool | None:
        return self._outbound_tool

    async def setup(self, context: IntegrationContext) -> None:
        registry = getattr(context.components, "tool_registry", None)
        if registry is None:
            return

        telegram = context.config.telegram
        telegram_bot_token = (
            telegram.bot_token.get_secret_value()
            if telegram and telegram.bot_token
            else None
        )

        if registry.has("vapi_outbound_call"):
            existing = registry.get("vapi_outbound_call")
            if isinstance(existing, VapiOutboundCallTool):
                self._outbound_tool = existing
        else:
            self._outbound_tool = VapiOutboundCallTool(
                context.config.vapi,
                telegram_bot_token=telegram_bot_token,
            )
            registry.register(self._outbound_tool)

        if not registry.has("vapi_call_status"):
            registry.register(VapiCallStatusTool(context.config.vapi))
        if not registry.has("vapi_end_call"):
            registry.register(VapiEndCallTool(context.config.vapi))

    async def on_startup(self, context: IntegrationContext) -> None:
        if (
            context.mode != "serve"
            or context.sessions_path is None
            or self._outbound_tool is None
        ):
            return
        self._outbound_tool.recover_pending_summaries(context.sessions_path)

    async def on_shutdown(self, context: IntegrationContext) -> None:
        _ = context
        if self._outbound_tool is not None:
            await self._outbound_tool.shutdown()
