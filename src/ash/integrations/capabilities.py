"""Capabilities integration contributor.

Spec contract: specs/subsystems.md (Integration Hooks), specs/capabilities.md.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ash.core.prompt import PromptContext
from ash.core.prompt_keys import TOOL_ROUTING_RULES_KEY
from ash.core.session import SessionState
from ash.integrations.runtime import IntegrationContext, IntegrationContributor
from ash.permissions import capability_allowed

if TYPE_CHECKING:
    from ash.capabilities import CapabilityProvider
    from ash.config import AshConfig, CapabilityProviderConfig

logger = logging.getLogger(__name__)


class CapabilitiesIntegration(IntegrationContributor):
    """Owns capability manager setup and RPC surface registration."""

    name = "capabilities"
    priority = 255

    async def setup(self, context: IntegrationContext) -> None:
        from ash.capabilities import create_capability_manager

        components = context.components
        manager = getattr(components, "capability_manager", None)
        providers = list(getattr(components, "capability_providers", None) or [])
        providers.extend(_build_configured_capability_providers(context.config))
        if manager is None:
            manager = await create_capability_manager()
            components.capability_manager = manager

        for provider in providers:
            try:
                definitions = await provider.definitions()
                allowed_definitions = [
                    definition
                    for definition in definitions
                    if capability_allowed(context.config, definition.id)
                ]
                blocked = [
                    definition.id
                    for definition in definitions
                    if not capability_allowed(context.config, definition.id)
                ]
                if blocked:
                    logger.info(
                        "capability_definitions_blocked",
                        extra={
                            "provider.namespace": getattr(
                                provider, "namespace", "unknown"
                            ),
                            "capability.ids": blocked,
                        },
                    )
                provider = _DefinitionFilteredProvider(provider, allowed_definitions)
                await manager.register_provider(provider)
            except Exception as e:
                logger.warning(
                    "capability_provider_register_failed",
                    extra={
                        "provider.namespace": getattr(provider, "namespace", "unknown"),
                        "error.message": str(e),
                    },
                )

        tool_registry = getattr(components, "tool_registry", None)
        if tool_registry is not None and hasattr(tool_registry, "has"):
            try:
                if tool_registry.has("use_skill") and hasattr(tool_registry, "get"):
                    skill_tool = tool_registry.get("use_skill")
                    setter = getattr(skill_tool, "set_capability_manager", None)
                    if callable(setter):
                        setter(manager)
            except Exception:
                logger.warning("capability_skill_wiring_failed", exc_info=True)

    def register_rpc_methods(self, server, context: IntegrationContext) -> None:
        from ash.rpc.methods.capability import register_capability_methods

        manager = getattr(context.components, "capability_manager", None)
        if manager is None:
            return
        register_capability_methods(server, manager)

    def augment_prompt_context(
        self,
        prompt_context: PromptContext,
        session: SessionState,
        context: IntegrationContext,
    ) -> PromptContext:
        _ = session
        _ = context
        lines = prompt_context.extra_context.setdefault(TOOL_ROUTING_RULES_KEY, [])
        if isinstance(lines, list):
            line = (
                "- For sensitive external integrations (email/calendar), use "
                "`ash-sb capability` so identity scope is enforced by "
                "`ASH_CONTEXT_TOKEN`; do not request raw credential env vars. "
                "Secret-like env vars are blocked by security policy."
            )
            if line not in lines:
                lines.append(line)
            blocked = sorted(context.config.capability_permissions.blocked)
            if blocked:
                block_line = (
                    "- Capability permissions currently block: "
                    f"{', '.join(blocked)}. Do not attempt these operations."
                )
                if block_line not in lines:
                    lines.append(block_line)
        return prompt_context


class _DefinitionFilteredProvider:
    """Provider wrapper that applies host capability allow/block policy."""

    def __init__(self, provider: CapabilityProvider, definitions: list) -> None:
        self._provider = provider
        self._definitions = definitions

    @property
    def namespace(self) -> str:
        return self._provider.namespace

    async def definitions(self):
        return self._definitions

    async def auth_begin(self, *args, **kwargs):
        return await self._provider.auth_begin(*args, **kwargs)

    async def auth_poll(self, *args, **kwargs):
        return await self._provider.auth_poll(*args, **kwargs)

    async def auth_complete(self, *args, **kwargs):
        return await self._provider.auth_complete(*args, **kwargs)

    async def invoke(self, *args, **kwargs):
        return await self._provider.invoke(*args, **kwargs)


def _build_configured_capability_providers(
    config: AshConfig,
) -> list[CapabilityProvider]:
    providers: list[CapabilityProvider] = []
    for provider_name, provider_config in sorted(config.capabilities.providers.items()):
        if not provider_config.enabled:
            continue
        try:
            provider = _create_capability_provider(
                provider_name=provider_name,
                config=provider_config,
            )
        except Exception as e:
            logger.warning(
                "capability_provider_load_failed",
                extra={
                    "provider_name": provider_name,
                    "namespace": provider_config.namespace or provider_name,
                    "command": provider_config.command,
                    "error.message": str(e),
                },
            )
            continue
        providers.append(provider)
    return providers


def _create_capability_provider(
    *,
    provider_name: str,
    config: CapabilityProviderConfig,
) -> CapabilityProvider:
    from ash.capabilities.providers import SubprocessCapabilityProvider

    return SubprocessCapabilityProvider(
        namespace=config.namespace or provider_name,
        command=config.command,
        timeout_seconds=config.timeout_seconds,
        env=config.env or None,
    )
