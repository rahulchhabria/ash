"""Web and local-search integration.

Spec contract: specs/subsystems.md (Integration Hooks), specs/web_search.md,
specs/local_search.md.
"""

from __future__ import annotations

from ash.core.prompt_keys import TOOL_ROUTING_RULES_KEY
from ash.integrations.runtime import IntegrationContext, IntegrationContributor
from ash.tools.builtin import (
    ExaSearchTool,
    GooglePlacesTool,
    HostedOpenAITool,
    WebFetchTool,
    WebSearchTool,
)
from ash.tools.builtin.search_cache import SearchCache


class SearchIntegration(IntegrationContributor):
    """Register search providers and contribute deterministic routing guidance."""

    name = "search"
    priority = 200

    async def setup(self, context: IntegrationContext) -> None:
        registry = context.components.tool_registry
        agent = getattr(context.components, "agent", None)
        supports_hosted_search = bool(
            getattr(agent, "supports_hosted_openai_tools", False)
        )
        if agent is None:
            model_config = context.config.get_model("default")
            supports_hosted_search = (
                model_config.provider in {"openai", "openai-oauth"}
                and not model_config.base_url
            )
        if supports_hosted_search and not registry.has("openai_web_search"):
            registry.register(
                HostedOpenAITool(
                    "openai_web_search",
                    "Use OpenAI hosted web search for current public information.",
                    {"type": "web_search", "search_context_size": "medium"},
                )
            )

        if not registry.has("web_fetch"):
            registry.register(
                WebFetchTool(
                    sandbox_config=context.config.sandbox,
                    workspace_path=context.config.workspace,
                    cache=SearchCache(maxsize=50, ttl=1800),
                )
            )

        places = context.config.google_places
        if (
            places
            and places.enabled
            and places.api_key
            and not registry.has("google_places")
        ):
            registry.register(
                GooglePlacesTool(
                    api_key=places.api_key.get_secret_value(),
                    sandbox_config=context.config.sandbox,
                    workspace_path=context.config.workspace,
                    cache=SearchCache(maxsize=100, ttl=900),
                    max_results=places.max_results,
                )
            )

        exa = context.config.exa_search
        if exa and exa.enabled and exa.api_key and not registry.has("exa_search"):
            registry.register(
                ExaSearchTool(
                    api_key=exa.api_key.get_secret_value(),
                    sandbox_config=context.config.sandbox,
                    workspace_path=context.config.workspace,
                    cache=SearchCache(maxsize=100, ttl=900),
                )
            )

        parallel = context.config.parallel_search
        if (
            parallel
            and parallel.enabled
            and parallel.api_key
            and not registry.has("web_search")
        ):
            # Search providers get an isolated bridge-network executor. Never pass
            # the shared no-network executor used by bash/file tools.
            registry.register(
                WebSearchTool(
                    api_key=parallel.api_key.get_secret_value(),
                    sandbox_config=context.config.sandbox,
                    workspace_path=context.config.workspace,
                    cache=SearchCache(maxsize=100, ttl=900),
                )
            )

    def augment_prompt_context(self, prompt_context, session, context):
        del session
        registry = context.components.tool_registry
        rules = list(prompt_context.extra_context.get(TOOL_ROUTING_RULES_KEY, []))
        rules.extend(
            [
                "Never invoke the `google` Gmail/Calendar skill for public web searches, businesses, maps, restaurants, store hours, or place discovery.",
                "If a search backend fails or is unavailable, immediately try another configured search backend before giving up.",
                "Use `browser` only for interactive, authenticated, or highly dynamic pages, or after search cannot resolve the lookup.",
            ]
        )
        if registry.has("openai_web_search"):
            rules.append(
                "For ordinary current public information, use hosted `openai_web_search` first."
            )
        if registry.has("google_places"):
            rules.append(
                "For a named local business, branch, address, phone number, or opening-hours question, use `google_places` before general web search; include city, neighborhood, address, or cross streets in the query when known."
            )
        fallbacks = [
            name for name in ("web_search", "exa_search") if registry.has(name)
        ]
        if fallbacks:
            rules.append(
                "Configured non-hosted search fallbacks, in order: "
                + ", ".join(f"`{name}`" for name in fallbacks)
                + "."
            )
            if not registry.has("openai_web_search"):
                rules.append(
                    f"For ordinary current public information, use `{fallbacks[0]}` first."
                )
        prompt_context.extra_context[TOOL_ROUTING_RULES_KEY] = rules
        return prompt_context
