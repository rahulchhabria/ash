from ash.skills.packages import scan_skill_packages


def test_scan_skill_packages(tmp_path) -> None:
    skill = tmp_path / "skills" / "bus"
    (skill / "scripts").mkdir(parents=True)
    (skill / "references").mkdir()
    (skill / "tests").mkdir()
    (skill / "scripts" / "fetch.py").write_text("print('ok')\n")
    (skill / "tests" / "test_fetch.py").write_text("def test_ok(): pass\n")
    (skill / "references" / "setup.md").write_text("setup\n")
    (skill / "SKILL.md").write_text(
        """---
description: Fetch bus arrivals
capabilities:
  - transit.511
allowed_tools:
  - bash
---

Use the script.
"""
    )

    packages = scan_skill_packages(tmp_path / "skills")

    assert packages == [
        {
            "name": "bus",
            "path": str(skill),
            "description": "Fetch bus arrivals",
            "capabilities": ["transit.511"],
            "allowed_tools": ["bash"],
            "scripts": ["scripts/fetch.py"],
            "has_tests": True,
            "has_setup_reference": True,
        }
    ]
