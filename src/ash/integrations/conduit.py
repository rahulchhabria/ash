"""Prompt routing for Ash's personal task orchestration agent."""

from __future__ import annotations

from ash.integrations.runtime import IntegrationContributor


class ConduitIntegration(IntegrationContributor):
    """Route action-oriented odd jobs through the checkpoint-capable coordinator."""

    name = "conduit"
    priority = 245

    def augment_prompt_context(self, prompt_context, session, context):
        _ = (session, context)
        extras = dict(prompt_context.extra_context)
        rules = list(extras.get("tool_routing_rules", []))
        rules.extend(
            [
                "Use `use_agent` with agent `conduit` for personal odd jobs that combine research, interactive web browsing, form workflows, reservations, or phone inquiries.",
                "The conduit agent must obtain a Telegram approval checkpoint before form submission, reservation, purchase, account mutation, or outbound Vapi call.",
                "Never route Conduit through Gmail or Google Calendar; those services are intentionally outside this setup.",
            ]
        )
        extras["tool_routing_rules"] = rules
        prompt_context.extra_context = extras
        return prompt_context
