"""Tests for explicit slash-command skill routing."""

from pathlib import Path
from unittest.mock import MagicMock

from ash.providers.telegram.handlers.message_handler import TelegramMessageHandler
from ash.skills.registry import SkillRegistry


def test_parse_slash_command_without_bot_mention() -> None:
    handler = TelegramMessageHandler.__new__(TelegramMessageHandler)
    handler._provider = MagicMock()
    handler._provider.bot_username = "ashbot"

    assert handler._parse_slash_command("/research compare tools") == (
        "/research",
        "compare tools",
    )


def test_parse_slash_command_with_matching_bot_mention() -> None:
    handler = TelegramMessageHandler.__new__(TelegramMessageHandler)
    handler._provider = MagicMock()
    handler._provider.bot_username = "ashbot"

    assert handler._parse_slash_command("/research@ashbot compare tools") == (
        "/research",
        "compare tools",
    )


def test_parse_slash_command_ignores_other_bot_mentions() -> None:
    handler = TelegramMessageHandler.__new__(TelegramMessageHandler)
    handler._provider = MagicMock()
    handler._provider.bot_username = "ashbot"

    assert handler._parse_slash_command("/research@otherbot compare tools") is None


def test_match_triggered_skill_resolves_registry_skill(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skill_dir = skills_dir / "triggered"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
description: Triggered skill
triggers:
  - /research
---

Run triggered behavior.
"""
    )

    registry = SkillRegistry()
    registry.discover(tmp_path, include_bundled=False)

    handler = TelegramMessageHandler.__new__(TelegramMessageHandler)
    handler._provider = MagicMock()
    handler._provider.bot_username = "ashbot"
    handler._skill_registry = registry

    matched = handler._match_triggered_skill("/research compare tools")
    assert matched is not None
    skill, command, arguments = matched
    assert skill.name == "triggered"
    assert command == "/research"
    assert arguments == "compare tools"


def test_bundled_research_command_exposes_hyphenated_triggers() -> None:
    skill_path = Path(
        "/home/rahul/GitHub/ash/src/ash/skills/bundled/research-command/SKILL.md"
    )
    text = skill_path.read_text()

    assert "/research-smoke" in text
    assert "/research-demo" in text
    assert "/research-full" in text
