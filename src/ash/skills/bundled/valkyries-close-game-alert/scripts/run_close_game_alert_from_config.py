#!/usr/bin/env python3
"""Launch the Valkyries close-game-alert daemon with Telegram config from ~/.ash/config.toml."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

CONFIG_PATH = Path.home() / ".ash" / "config.toml"
SCRIPT_PATH = (
    Path.home()
    / ".ash"
    / "workspace"
    / "skills"
    / "valkyries-close-game-alert"
    / "scripts"
    / "wnba_close_game_alert.py"
)
DEFAULT_ASH_PYTHON = Path.home() / "GitHub" / "ash-main" / ".venv" / "bin" / "python3"


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    with CONFIG_PATH.open("rb") as handle:
        payload = tomllib.load(handle)
    return payload if isinstance(payload, dict) else {}


def _ash_python() -> str:
    configured = os.environ.get("ASH_PYTHON", "").strip()
    if configured:
        return configured
    if DEFAULT_ASH_PYTHON.is_file():
        return str(DEFAULT_ASH_PYTHON)
    return sys.executable


def main() -> int:
    config = _load_config()
    env_config = config.get("env", {})
    telegram_config = config.get("telegram", {})
    sandbox_env = config.get("sandbox", {}).get("env", {})
    close_game = config.get("skills", {}).get("valkyries-close-game-alert", {})

    token = (
        os.environ.get("TELEGRAM_BOT_TOKEN")
        or close_game.get("TELEGRAM_BOT_TOKEN")
        or env_config.get("TELEGRAM_BOT_TOKEN")
        or sandbox_env.get("TELEGRAM_BOT_TOKEN")
        or telegram_config.get("bot_token")
        or ""
    ).strip()
    chat_id = (
        os.environ.get("TELEGRAM_CHAT_ID")
        or close_game.get("TELEGRAM_CHAT_ID")
        or env_config.get("TELEGRAM_CHAT_ID")
        or env_config.get("telegram_chat_id")
        or sandbox_env.get("TELEGRAM_CHAT_ID")
        or sandbox_env.get("telegram_chat_id")
        or ""
    ).strip()

    env = os.environ.copy()
    if token:
        env["TELEGRAM_BOT_TOKEN"] = token
    if chat_id:
        env["TELEGRAM_CHAT_ID"] = chat_id

    team = os.environ.get("CLOSE_GAME_TEAM", "Valkyries")
    tz = os.environ.get("CLOSE_GAME_TIMEZONE", "America/Los_Angeles")
    poll_seconds = os.environ.get("CLOSE_GAME_POLL_SECONDS", "120")
    idle_poll_seconds = os.environ.get("CLOSE_GAME_IDLE_POLL_SECONDS", "21600")
    quiet_prestart_seconds = os.environ.get("CLOSE_GAME_QUIET_PRESTART_SECONDS", "900")
    threshold = os.environ.get("CLOSE_GAME_THRESHOLD", "6")
    minutes_left = os.environ.get("CLOSE_GAME_MINUTES_LEFT", "5")

    cmd = [
        _ash_python(),
        "-u",
        str(SCRIPT_PATH),
        "daemon",
        "--team",
        team,
        "--tz",
        tz,
        "--poll-seconds",
        poll_seconds,
        "--idle-poll-seconds",
        idle_poll_seconds,
        "--quiet-prestart-seconds",
        quiet_prestart_seconds,
        "--threshold",
        threshold,
        "--minutes-left",
        minutes_left,
    ]
    completed = subprocess.run(cmd, env=env, check=False)  # noqa: S603
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
