"""Capability permission helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ash.config import AshConfig


def capability_allowed(config: AshConfig, capability_id: str) -> bool:
    permissions = config.capability_permissions
    if capability_id in permissions.blocked:
        return False
    if permissions.allowed and capability_id not in permissions.allowed:
        return False
    return True
