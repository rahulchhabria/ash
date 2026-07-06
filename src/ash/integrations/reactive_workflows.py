"""Reactive-workflow integration contributor.

Generic, config-driven signal->workflow routing. When an inbound provider
message matches a configured rule (prefix or regex), a structured instruction
block is prepended to the message so the agent deterministically routes the
turn to a named skill or built-in agent. This is the event-driven counterpart
to the scheduler's time-driven autonomy, and the generic form of the bespoke
``email_forward_summary`` / ``close_game_alert`` integrations.

Prompt augmentation happens by transforming the inbound message text
(``preprocess_incoming_message``), the same mechanism the bespoke integrations
use — no prompt-fragment injection into prompt-building code.

Spec contract: specs/subsystems.md (Integration Hooks),
specs/reactive_workflows.md.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ash.integrations.runtime import IntegrationContext, IntegrationContributor

if TYPE_CHECKING:
    from ash.providers.base import IncomingMessage

logger = logging.getLogger("reactive_workflows")

CONTEXT_HEADER = "Reactive-workflow trigger"
CONTEXT_FOOTER = "End reactive-workflow trigger"


@dataclass(slots=True)
class _CompiledRule:
    name: str
    prefix: str | None
    regex: re.Pattern[str] | None
    skill: str | None
    agent: str | None
    instruction: str | None
    chat_types: frozenset[str]

    def matches(self, text: str, chat_type: str | None) -> bool:
        if self.chat_types and (chat_type or "") not in self.chat_types:
            return False
        if self.prefix and text.lstrip().startswith(self.prefix):
            return True
        if self.regex and self.regex.search(text):
            return True
        return False


class ReactiveWorkflowIntegration(IntegrationContributor):
    """Route matching inbound messages to a skill/agent via structured context."""

    name = "reactive_workflows"
    # After the bespoke context injectors so a specific match wins if both fire.
    priority = 180

    def __init__(self) -> None:
        self._enabled: bool = False
        self._rules: list[_CompiledRule] = []

    async def setup(self, context: IntegrationContext) -> None:
        config = context.config.reactive_workflows
        if not config.enabled:
            return

        compiled: list[_CompiledRule] = []
        for rule in config.rules:
            if not rule.match_prefix and not rule.match_regex:
                logger.warning(
                    "reactive_workflow_rule_skipped",
                    extra={"rule.name": rule.name, "reason": "no_matcher"},
                )
                continue
            if not rule.skill and not rule.agent and not rule.instruction:
                logger.warning(
                    "reactive_workflow_rule_skipped",
                    extra={"rule.name": rule.name, "reason": "no_action"},
                )
                continue
            pattern: re.Pattern[str] | None = None
            if rule.match_regex:
                try:
                    pattern = re.compile(rule.match_regex)
                except re.error as exc:
                    logger.warning(
                        "reactive_workflow_rule_skipped",
                        extra={
                            "rule.name": rule.name,
                            "reason": "invalid_regex",
                            "error.message": str(exc),
                        },
                    )
                    continue
            compiled.append(
                _CompiledRule(
                    name=rule.name,
                    prefix=rule.match_prefix,
                    regex=pattern,
                    skill=rule.skill,
                    agent=rule.agent,
                    instruction=rule.instruction,
                    chat_types=frozenset(rule.chat_types),
                )
            )

        if not compiled:
            logger.warning(
                "reactive_workflows_disabled", extra={"reason": "no_valid_rules"}
            )
            return

        self._rules = compiled
        self._enabled = True
        logger.info(
            "reactive_workflows_ready",
            extra={"reactive_workflows.rule_count": len(compiled)},
        )

    async def preprocess_incoming_message(
        self,
        message: IncomingMessage,
        context: IntegrationContext,
    ) -> IncomingMessage:
        if not self._enabled or not message.text:
            return message

        chat_type = message.metadata.get("chat_type")
        rule = self._first_match(message.text, chat_type)
        if rule is None:
            return message

        block = self._render_context_block(rule)
        message.text = f"{block}\n\n{message.text}".strip()
        message.metadata = {
            **message.metadata,
            "reactive_workflow.rule": rule.name,
        }
        logger.info(
            "reactive_workflow_matched",
            extra={
                "reactive_workflow.rule": rule.name,
                "reactive_workflow.skill": rule.skill,
                "reactive_workflow.agent": rule.agent,
            },
        )
        return message

    def _first_match(self, text: str, chat_type: str | None) -> _CompiledRule | None:
        for rule in self._rules:
            if rule.matches(text, chat_type):
                return rule
        return None

    def _render_context_block(self, rule: _CompiledRule) -> str:
        lines = [f"--- {CONTEXT_HEADER} ---", f"rule: {rule.name}"]
        if rule.skill:
            lines.append(
                f"action: Invoke the `{rule.skill}` skill via use_skill to handle "
                "this message."
            )
        elif rule.agent:
            lines.append(
                f"action: Delegate this message to the `{rule.agent}` agent via "
                "use_agent."
            )
        if rule.instruction:
            lines.append(f"guidance: {rule.instruction}")
        lines.append(
            "note: This routing is a suggestion triggered by a matching rule; "
            "use judgment if the message clearly needs a different response."
        )
        lines.append(f"--- {CONTEXT_FOOTER} ---")
        return "\n".join(lines)
