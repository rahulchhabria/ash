"""Skill package metadata helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def scan_skill_packages(root: Path) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    packages: list[dict[str, Any]] = []
    for skill_md in sorted(root.glob("*/SKILL.md")):
        skill_dir = skill_md.parent
        text = skill_md.read_text(encoding="utf-8", errors="replace")
        packages.append(
            {
                "name": skill_dir.name,
                "path": str(skill_dir),
                "description": _frontmatter_value(text, "description"),
                "capabilities": _frontmatter_list(text, "capabilities"),
                "allowed_tools": _frontmatter_list(text, "allowed_tools"),
                "scripts": sorted(
                    str(p.relative_to(skill_dir))
                    for p in skill_dir.glob("scripts/*")
                    if p.is_file()
                ),
                "has_tests": any(skill_dir.glob("tests/test_*.py")),
                "has_setup_reference": (skill_dir / "references" / "setup.md").exists(),
            }
        )
    return packages


def _frontmatter(text: str) -> str:
    if not text.startswith("---\n"):
        return ""
    end = text.find("\n---", 4)
    return text[4:end] if end >= 0 else ""


def _frontmatter_value(text: str, key: str) -> str:
    prefix = f"{key}:"
    for line in _frontmatter(text).splitlines():
        if line.startswith(prefix):
            return line.split(":", 1)[1].strip().strip('"')
    return ""


def _frontmatter_list(text: str, key: str) -> list[str]:
    lines = _frontmatter(text).splitlines()
    values: list[str] = []
    in_key = False
    for line in lines:
        if line.startswith(f"{key}:"):
            in_key = True
            tail = line.split(":", 1)[1].strip()
            if tail.startswith("[") and tail.endswith("]"):
                return [
                    part.strip().strip('"')
                    for part in tail[1:-1].split(",")
                    if part.strip()
                ]
            continue
        if in_key:
            if line.startswith("  - "):
                values.append(line[4:].strip().strip('"'))
                continue
            if line and not line.startswith(" "):
                break
    return values
