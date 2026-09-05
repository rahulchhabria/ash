"""Local control dashboard and event intake routes."""

from __future__ import annotations

import base64
import binascii
import hmac
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator

from ash.events import event_to_dict, read_events, record_event, validate_event_metadata
from ash.skills.packages import scan_skill_packages

router = APIRouter()


class EventPayload(BaseModel):
    source: str = Field(default="external", min_length=1, max_length=100)
    kind: str = Field(default="event", min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(default="", max_length=50_000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def _validate_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        return validate_event_metadata(value)


def _config(request: Request):
    config = getattr(request.app.state, "config", None)
    if config is None:
        raise HTTPException(status_code=503, detail="config unavailable")
    return config


def _authorization_matches(expected_token: str, authorization: str | None) -> bool:
    if not authorization:
        return False
    candidate = ""
    if authorization.startswith("Bearer "):
        candidate = authorization.removeprefix("Bearer ")
    elif authorization.startswith("Basic "):
        try:
            decoded = base64.b64decode(
                authorization.removeprefix("Basic "), validate=True
            ).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return False
        _, separator, candidate = decoded.partition(":")
        if not separator:
            return False
    return hmac.compare_digest(candidate, expected_token)


def _require_event_auth(request: Request, authorization: str | None) -> None:
    config = _config(request)
    if not config.event_router.auth_required:
        return
    token = config.event_router.bearer_token
    if token is None:
        raise HTTPException(
            status_code=503,
            detail="event router authentication is required but not configured",
        )
    if not _authorization_matches(token.get_secret_value(), authorization):
        raise HTTPException(
            status_code=401,
            detail="invalid authorization",
            headers={"WWW-Authenticate": 'Basic realm="Ash Control", Bearer'},
        )


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request, authorization: str | None = Header(default=None)
) -> str:
    config = _config(request)
    if not config.dashboard.enabled:
        raise HTTPException(status_code=404, detail="dashboard disabled")
    _require_event_auth(request, authorization)
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Pigeon Control</title>
  <style>
    body { margin: 0; font-family: system-ui, sans-serif; background: #111; color: #f4f4f0; }
    main { max-width: 1050px; margin: 0 auto; padding: 24px; }
    h1 { font-size: 28px; margin: 0 0 18px; }
    h2 { font-size: 16px; margin: 22px 0 8px; color: #b9d8cd; }
    pre { background: #1b1d1b; border: 1px solid #333b36; border-radius: 6px; padding: 12px; overflow: auto; }
    button { border: 1px solid #47564e; background: #202820; color: #fff; border-radius: 6px; padding: 8px 12px; cursor: pointer; }
  </style>
</head>
<body>
  <main>
    <h1>Pigeon Control</h1>
    <button onclick="load()">Refresh</button>
    <h2>Status</h2><pre id="status">Loading...</pre>
    <h2>Events</h2><pre id="events">Loading...</pre>
    <h2>Skills</h2><pre id="skills">Loading...</pre>
  </main>
  <script>
    async function get(path) { return JSON.stringify(await (await fetch(path)).json(), null, 2); }
    async function load() {
      status.textContent = await get('/dashboard/status');
      events.textContent = await get('/events?limit=20');
      skills.textContent = await get('/dashboard/skills');
    }
    load();
  </script>
</body>
</html>"""


@router.get("/dashboard/status")
async def dashboard_status(
    request: Request, authorization: str | None = Header(default=None)
) -> dict[str, Any]:
    config = _config(request)
    if not config.dashboard.enabled:
        raise HTTPException(status_code=404, detail="dashboard disabled")
    _require_event_auth(request, authorization)
    runtime = getattr(request.app.state, "integration_runtime", None)
    health = asdict(runtime.health_snapshot()) if runtime else None
    return {
        "status": "ok",
        "context_firewall": config.context_firewall.model_dump(),
        "capability_permissions": config.capability_permissions.model_dump(),
        "event_router": {
            "enabled": config.event_router.enabled,
            "auth_required": config.event_router.auth_required,
            "auth_configured": config.event_router.bearer_token is not None,
            "allowed_sources": config.event_router.allowed_sources,
        },
        "deepagents": config.deepagents.model_dump(),
        "vapi": {"enabled": config.vapi.enabled},
        "integrations": health,
    }


@router.get("/dashboard/skills")
async def dashboard_skills(
    request: Request, authorization: str | None = Header(default=None)
) -> list[dict[str, Any]]:
    config = _config(request)
    if not config.dashboard.enabled:
        raise HTTPException(status_code=404, detail="dashboard disabled")
    _require_event_auth(request, authorization)
    if not config.skill_packages.enabled:
        return []
    return scan_skill_packages(Path(config.workspace) / "skills")


@router.get("/events")
async def events(
    request: Request,
    limit: int = 50,
    authorization: str | None = Header(default=None),
) -> list[dict[str, Any]]:
    config = _config(request)
    if not config.event_router.enabled:
        raise HTTPException(status_code=404, detail="event router disabled")
    _require_event_auth(request, authorization)
    return read_events(limit=max(1, min(limit, 200)))


@router.post("/events")
async def post_event(
    payload: EventPayload,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    config = _config(request)
    if not config.event_router.enabled:
        raise HTTPException(status_code=404, detail="event router disabled")
    _require_event_auth(request, authorization)
    if (
        config.event_router.allowed_sources
        and payload.source not in config.event_router.allowed_sources
    ):
        raise HTTPException(status_code=403, detail="source not allowed")
    event = record_event(
        source=payload.source,
        kind=payload.kind,
        title=payload.title,
        body=payload.body[: config.event_router.max_body_chars],
        metadata=payload.metadata,
    )
    return {"ok": True, "event": event_to_dict(event)}
