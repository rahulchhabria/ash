"""Vapi webhook routes."""

from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from ash.integrations.vapi import parse_vapi_webhook

logger = logging.getLogger("vapi")
router = APIRouter()


@router.post("/vapi/webhook", include_in_schema=False)
@router.post("/webhooks/vapi")
async def vapi_webhook(
    request: Request, background_tasks: BackgroundTasks
) -> dict[str, str]:
    """Receive Vapi server messages and submit voicemails to Ash."""
    config = getattr(request.app.state, "config", None)
    vapi_config = getattr(config, "vapi", None)
    if vapi_config is None or not vapi_config.enabled:
        raise HTTPException(status_code=404, detail="Vapi webhook is not enabled")

    _verify_secret(
        request,
        vapi_config.webhook_secret,
        auth_required=vapi_config.auth_required,
    )

    try:
        payload = await request.json()
    except Exception as exc:
        logger.warning("vapi_webhook_invalid_json")
        raise HTTPException(status_code=400, detail="Invalid JSON") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")

    try:
        result = parse_vapi_webhook(payload, config=vapi_config)
    except ValueError as exc:
        logger.warning("vapi_webhook_misconfigured", extra={"error.message": str(exc)})
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if result.ignored:
        logger.info(
            "vapi_webhook_ignored",
            extra={"vapi.ignore_reason": result.reason},
        )
        return {"status": "ignored", "reason": result.reason or "ignored"}

    server = getattr(request.app.state, "server", None)
    if server is None:
        raise HTTPException(status_code=503, detail="Ash server is unavailable")
    handler = await server.get_telegram_handler()
    if handler is None:
        raise HTTPException(status_code=503, detail="Telegram handler is unavailable")

    assert result.message is not None
    background_tasks.add_task(_handle_vapi_message, handler, result.message)
    return {"status": "accepted"}


def _verify_secret(
    secret_header_source: Request,
    secret: Any,
    *,
    auth_required: bool = True,
) -> None:
    if secret is None:
        if auth_required:
            raise HTTPException(
                status_code=503,
                detail="Vapi webhook authentication is required but not configured",
            )
        return

    expected = secret.get_secret_value()
    authorization = secret_header_source.headers.get("authorization", "")
    bearer = ""
    if authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()
    legacy = secret_header_source.headers.get("x-vapi-secret", "").strip()

    if hmac.compare_digest(bearer, expected) or hmac.compare_digest(legacy, expected):
        return
    raise HTTPException(status_code=401, detail="Invalid Vapi webhook credentials")


async def _handle_vapi_message(handler: Any, message: Any) -> None:
    logger.info(
        "vapi_voicemail_received",
        extra={
            "vapi.call_id": message.metadata.get("vapi.call_id"),
            "vapi.caller_number": message.metadata.get("vapi.caller_number"),
        },
    )
    await handler.handle_message(message)
