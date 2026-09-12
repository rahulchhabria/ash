"""Portable, read-only host environment discovery."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ASH_RUNTIME_COMMANDS = ("python3", "uv", "git", "docker", "systemctl")
OMARCHY_DESKTOP_COMMANDS = ("hyprctl", "wl-copy", "notify-send", "grim", "slurp")


@dataclass(frozen=True, slots=True)
class HostEnvironment:
    """Sanitized host facts used by diagnostics and future integrations."""

    platform: str
    distro_id: str | None
    distro_like: tuple[str, ...]
    session_type: str | None
    desktop: str | None
    wayland_display: str | None
    commands: frozenset[str]
    python_version: tuple[int, int, int] | None = None
    docker_ready: bool | None = None
    systemd_user_ready: bool | None = None
    omarchy_install_detected: bool = False

    @property
    def is_arch_family(self) -> bool:
        return self.distro_id in {"arch", "omarchy"} or bool(
            {"arch", "omarchy"}.intersection(self.distro_like)
        )

    @property
    def is_omarchy(self) -> bool:
        return (
            self.distro_id == "omarchy"
            or "omarchy" in self.commands
            or self.omarchy_install_detected
        )

    def has_command(self, command: str) -> bool:
        return command in self.commands


def _read_os_release(path: Path = Path("/etc/os-release")) -> dict[str, str]:
    """Parse os-release data without executing host commands."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, PermissionError, OSError):
        return {}

    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value.strip().strip('"').strip("'")
    return values


def detect_host_environment() -> HostEnvironment:
    """Return non-sensitive host facts relevant to Ash runtime selection."""
    release = _read_os_release()
    distro_id = release.get("ID", "").strip().lower() or None
    distro_like = tuple(
        item.lower() for item in release.get("ID_LIKE", "").split() if item
    )
    candidates = (*ASH_RUNTIME_COMMANDS, *OMARCHY_DESKTOP_COMMANDS, "omarchy")
    commands = frozenset(name for name in candidates if shutil.which(name))
    docker_ready = _probe_command(("docker", "info")) if "docker" in commands else None
    systemd_user_ready = (
        _probe_command(("systemctl", "--user", "show-environment"))
        if "systemctl" in commands
        else None
    )
    omarchy_install_detected = any(
        path.exists()
        for path in (
            Path.home() / ".local/share/omarchy",
            Path("/usr/local/share/omarchy"),
        )
    )
    return HostEnvironment(
        platform=sys.platform,
        distro_id=distro_id,
        distro_like=distro_like,
        session_type=os.environ.get("XDG_SESSION_TYPE") or None,
        desktop=os.environ.get("XDG_CURRENT_DESKTOP") or None,
        wayland_display=os.environ.get("WAYLAND_DISPLAY") or None,
        commands=commands,
        python_version=tuple(sys.version_info[:3]),
        docker_ready=docker_ready,
        systemd_user_ready=systemd_user_ready,
        omarchy_install_detected=omarchy_install_detected,
    )


def _probe_command(command: tuple[str, ...]) -> bool:
    """Return whether a fixed, read-only readiness probe succeeds."""
    try:
        result = subprocess.run(  # noqa: S603 - callers pass fixed readiness probes
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0
