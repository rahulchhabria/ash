"""Vapi outbound calling tool."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from ash.config.models import VapiConfig
from ash.sessions.types import OperationState
from ash.tools.base import Tool, ToolContext, ToolResult

E164_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")
UNRESOLVED_PLACEHOLDER_RE = re.compile(r"(?:<[^<>]{1,80}>|{{[^{}]{1,80}}})")
CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x18\x1a-\x1f\x7f]")
POLL_INTERVAL_SECONDS = 5
POLL_TIMEOUT_SECONDS = 15 * 60
CALL_RETRY_GUARD_SECONDS = 60 * 60

logger = logging.getLogger(__name__)
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()
_CALL_CREATION_LOCK = asyncio.Lock()
ACTIVE_CALL_STATUSES = {"queued", "ringing", "in-progress", "forwarding"}


def _canonical_call_request(
    *,
    customer_number: str,
    objective: str,
    business_name: str,
    call_context: str,
    customer_name: str,
    allow_ivr_navigation: bool,
    voicemail_message: str,
    retry_operation_id: str,
) -> dict[str, Any]:
    return {
        "action": "vapi_call",
        "customer_number": customer_number.strip(),
        "objective": _clean_voice_text(objective, limit=2000),
        "business_name": _clean_voice_text(business_name, limit=300),
        "context": _clean_voice_text(call_context, limit=4000),
        "customer_name": _clean_voice_text(customer_name, limit=300),
        "allow_ivr_navigation": allow_ivr_navigation,
        "voicemail_message": _clean_voice_text(voicemail_message, limit=500),
        "retry_operation_id": retry_operation_id.strip(),
    }


def _canonical_request_from_mapping(value: dict[str, Any]) -> dict[str, Any]:
    return _canonical_call_request(
        customer_number=str(value.get("customer_number") or ""),
        objective=str(value.get("objective") or ""),
        business_name=str(value.get("business_name") or ""),
        call_context=str(value.get("context") or ""),
        customer_name=str(value.get("customer_name") or ""),
        allow_ivr_navigation=value.get("allow_ivr_navigation") is True,
        voicemail_message=str(value.get("voicemail_message") or ""),
        retry_operation_id=str(value.get("retry_operation_id") or ""),
    )


def _call_idempotency_key(session_id: str, approval_request: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            {"session_id": session_id, "approval": approval_request},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def _validated_approval(
    context: ToolContext, expected_request: dict[str, Any]
) -> tuple[str, dict[str, Any]] | None:
    grant = context.metadata.get("approval_grant")
    if not isinstance(grant, dict) or context.session_manager is None:
        return None
    checkpoint_id = str(grant.get("checkpoint_id") or "")
    request = grant.get("approval_request")
    if not checkpoint_id or not isinstance(request, dict):
        return None
    if _canonical_request_from_mapping(request) != expected_request:
        return None
    record = context.session_manager.get_pending_checkpoint(checkpoint_id)
    if (
        record is None
        or record.status != "claimed"
        or record.approval_consumed_at is not None
        or record.checkpoint is None
        or record.checkpoint.get("approval_request") != request
    ):
        return None
    return checkpoint_id, request


def _call_matches_operation(
    call: dict[str, Any], conversation_id: str, operation_key: str
) -> bool:
    overrides = call.get("assistantOverrides")
    if not isinstance(overrides, dict):
        return False
    values = overrides.get("variableValues")
    if not isinstance(values, dict):
        return False
    return (
        values.get("ash_conversation_id") == conversation_id
        and values.get("ash_operation_key") == operation_key
    )


class VapiOutboundCallTool(Tool):
    """Place one explicitly approved outbound call through Vapi."""

    def __init__(
        self,
        config: VapiConfig,
        *,
        telegram_bot_token: str | None = None,
    ) -> None:
        self._config = config
        self._telegram_bot_token = telegram_bot_token

    @property
    def name(self) -> str:
        return "vapi_outbound_call"

    @property
    def description(self) -> str:
        return (
            "Place an outbound phone call through Vapi for a basic inquiry. "
            "This is consequential: use only after showing the exact destination "
            "and objective to the user and receiving explicit approval."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "customer_number": {
                    "type": "string",
                    "description": "Destination phone number in E.164 format.",
                },
                "objective": {
                    "type": "string",
                    "description": "The precise inquiry the voice assistant should make.",
                },
                "business_name": {
                    "type": "string",
                    "description": "Business or place being called.",
                },
                "context": {
                    "type": "string",
                    "description": "Bounded context needed to conduct the inquiry.",
                },
                "customer_name": {
                    "type": "string",
                    "description": (
                        "Optional name of the person being called. This is the "
                        "recipient, not the Telegram user placing the call."
                    ),
                },
                "allow_ivr_navigation": {
                    "type": "boolean",
                    "description": (
                        "Allow DTMF only for ordinary, non-consequential IVR routing "
                        "needed to reach the approved person or department. This "
                        "permission must be disclosed in the call approval."
                    ),
                },
                "voicemail_message": {
                    "type": "string",
                    "description": (
                        "Exact short message to leave only when the user explicitly "
                        "approved leaving voicemail. Omit to hang up silently."
                    ),
                },
                "retry_operation_id": {
                    "type": "string",
                    "description": (
                        "Existing call operation ID to retry. Set only after the user "
                        "explicitly approves retrying that completed or failed call."
                    ),
                },
            },
            "required": [
                "customer_number",
                "objective",
                "allow_ivr_navigation",
            ],
        }

    async def execute(
        self, input_data: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        if context.provider != "telegram":
            return ToolResult.error(
                "Outbound Vapi calls are restricted to Telegram-dispatched tasks"
            )
        if not self._config.enabled:
            return ToolResult.error("Vapi is disabled")

        number = str(input_data.get("customer_number") or "").strip()
        if not E164_RE.fullmatch(number):
            return ToolResult.error("customer_number must use E.164 format")
        objective = _clean_voice_text(input_data.get("objective"), limit=2000)
        if not objective:
            return ToolResult.error("objective is required")
        if context.is_cancelled:
            return ToolResult.error("Call cancelled before placement")

        retry_operation_id = str(input_data.get("retry_operation_id") or "").strip()
        business_name = _clean_voice_text(input_data.get("business_name"), limit=300)
        call_context = _clean_voice_text(input_data.get("context"), limit=4000)
        customer_name = _clean_voice_text(input_data.get("customer_name"), limit=300)
        allow_ivr_navigation = input_data.get("allow_ivr_navigation") is True
        voicemail_message = _clean_voice_text(
            input_data.get("voicemail_message"), limit=500
        )
        for field_name, value in (
            ("objective", objective),
            ("business_name", business_name),
            ("context", call_context),
            ("customer_name", customer_name),
            ("voicemail_message", voicemail_message),
        ):
            if UNRESOLVED_PLACEHOLDER_RE.search(value):
                return ToolResult.error(
                    f"{field_name} contains an unresolved placeholder; replace it "
                    "with the approved call detail before placing the call"
                )

        approval_request = _canonical_call_request(
            customer_number=number,
            objective=objective,
            business_name=business_name,
            call_context=call_context,
            customer_name=customer_name,
            allow_ivr_navigation=allow_ivr_navigation,
            voicemail_message=voicemail_message,
            retry_operation_id=retry_operation_id,
        )
        approval = _validated_approval(context, approval_request)
        if approval is None:
            return ToolResult.error(
                "A matching, unconsumed Telegram checkpoint approval is required "
                "before placing this call"
            )
        checkpoint_id, raw_approval_request = approval
        session_manager = context.session_manager
        assert session_manager is not None

        idempotency_key = _call_idempotency_key(
            context.session_id or "", approval_request
        )
        existing = session_manager.latest_operation(
            "vapi_call", idempotency_key=idempotency_key
        )
        if existing is not None:
            age_seconds = (datetime.now(UTC) - existing.created_at).total_seconds()
            retry_matches = retry_operation_id == existing.operation_id
            if age_seconds < CALL_RETRY_GUARD_SECONDS and not retry_matches:
                if not session_manager.consume_checkpoint_approval(
                    checkpoint_id, raw_approval_request
                ):
                    return ToolResult.error("The call approval was already consumed")
                return ToolResult.success(
                    json.dumps(
                        {
                            "call_id": existing.operation_id,
                            "status": existing.status,
                            "reused_existing_operation": True,
                            "message": (
                                "No duplicate call was placed. Use vapi_call_status "
                                "for an update, or obtain explicit approval to retry."
                            ),
                        },
                        indent=2,
                    )
                )

        if retry_operation_id:
            retry_operation = session_manager.get_operation(retry_operation_id)
            if (
                retry_operation is None
                or retry_operation.kind != "vapi_call"
                or retry_operation.destination != number
            ):
                return ToolResult.error(
                    "retry_operation_id is not a call to this destination in the "
                    "current conversation"
                )

        variables = {
            "objective": objective,
            "ash_objective": objective,
            "ash_business_name": business_name,
            "ash_context": call_context,
            "ash_customer_name": customer_name,
            "ash_ivr_navigation": (
                "routing-only" if allow_ivr_navigation else "disabled"
            ),
            "ash_conversation_id": context.session_id or "",
            "ash_operation_key": idempotency_key,
        }
        if self._config.dry_run:
            if not session_manager.consume_checkpoint_approval(
                checkpoint_id, raw_approval_request
            ):
                return ToolResult.error("The call approval was already consumed")
            summary = {
                "call_id": None,
                "status": "dry_run",
                "customer_number": number,
                "business_name": variables["ash_business_name"] or None,
                "objective": objective,
                "ivr_navigation": allow_ivr_navigation,
            }
            return ToolResult.success(json.dumps(summary, indent=2))

        api_key = self._config.api_key
        if api_key is None:
            return ToolResult.error("VAPI_API_KEY is not configured")
        if not self._config.assistant_id or not self._config.phone_number_id:
            return ToolResult.error(
                "Vapi outbound calling requires assistant_id and phone_number_id"
            )

        assistant_overrides: dict[str, Any] = {
            "variableValues": variables,
            "firstMessageMode": "assistant-speaks-first-with-model-generated-message",
        }
        if voicemail_message:
            assistant_overrides["voicemailMessage"] = voicemail_message
        payload = {
            "assistantId": self._config.assistant_id,
            "phoneNumberId": self._config.phone_number_id,
            "customer": {"number": number},
            "assistantOverrides": assistant_overrides,
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                headers = {
                    "Authorization": f"Bearer {api_key.get_secret_value()}",
                    "Content-Type": "application/json",
                }
                base_url = self._config.base_url.rstrip("/")
                async with _CALL_CREATION_LOCK:
                    response = await client.get(
                        f"{base_url}/call",
                        headers=headers,
                        params={"limit": 20},
                    )
                    response.raise_for_status()
                    active_call = _active_call_to_number(
                        response.json(), self._config, number
                    )
                    if active_call is not None:
                        active_call_id = str(active_call.get("id") or "")
                        if active_call_id and _call_matches_operation(
                            active_call,
                            context.session_id or "",
                            idempotency_key,
                        ):
                            if not session_manager.consume_checkpoint_approval(
                                checkpoint_id, raw_approval_request
                            ):
                                return ToolResult.error(
                                    "The call approval was already consumed"
                                )
                            session_manager.record_operation(
                                OperationState(
                                    kind="vapi_call",
                                    operation_id=active_call_id,
                                    status=str(active_call.get("status") or "active"),
                                    idempotency_key=idempotency_key,
                                    destination=number,
                                    objective=objective,
                                )
                            )
                            return ToolResult.success(
                                json.dumps(
                                    {
                                        "call_id": active_call_id,
                                        "status": str(
                                            active_call.get("status") or "active"
                                        ),
                                        "reused_existing_operation": True,
                                    },
                                    indent=2,
                                )
                            )
                        return ToolResult.error(
                            "A call to this number is already active; no duplicate "
                            "was placed"
                        )
                    if context.is_cancelled:
                        return ToolResult.error("Call cancelled before placement")
                    if not session_manager.consume_checkpoint_approval(
                        checkpoint_id, raw_approval_request
                    ):
                        return ToolResult.error(
                            "The call approval was already consumed"
                        )
                    response = await client.post(
                        f"{base_url}/call",
                        headers=headers,
                        json=payload,
                    )
                    response.raise_for_status()
                    result = response.json()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500]
            return ToolResult.error(
                f"Vapi rejected the call ({exc.response.status_code}): {detail}"
            )
        except (httpx.HTTPError, ValueError) as exc:
            return ToolResult.error(f"Vapi call creation failed: {exc}")
        if not isinstance(result, dict):
            return ToolResult.error("Vapi returned an unexpected response")
        call_id = str(result.get("id") or "").strip()
        if call_id:
            if session_manager is not None:
                session_manager.record_operation(
                    OperationState(
                        kind="vapi_call",
                        operation_id=call_id,
                        status=str(result.get("status") or "queued"),
                        idempotency_key=idempotency_key,
                        destination=number,
                        objective=objective,
                        metadata={"business_name": business_name},
                    )
                )
            self._start_summary_watcher(
                call_id=call_id,
                chat_id=context.chat_id or self._config.telegram_chat_id,
                customer_number=number,
                business_name=variables["ash_business_name"],
                objective=objective,
                session_manager=session_manager,
            )
        summary = {
            "call_id": call_id or None,
            "status": result.get("status") or "queued",
            "business_name": variables["ash_business_name"] or None,
            "ivr_navigation": allow_ivr_navigation,
            "summary_delivery": (
                "telegram"
                if call_id
                and self._telegram_bot_token
                and (context.chat_id or self._config.telegram_chat_id)
                else None
            ),
        }
        return ToolResult.success(json.dumps(summary, indent=2))

    def _start_summary_watcher(
        self,
        *,
        call_id: str,
        chat_id: str | None,
        customer_number: str,
        business_name: str,
        objective: str,
        session_manager: Any = None,
    ) -> None:
        if not self._telegram_bot_token or not chat_id:
            logger.warning(
                "vapi_summary_delivery_disabled",
                extra={"vapi.call_id": call_id},
            )
            return
        task = asyncio.create_task(
            self._watch_call(
                call_id=call_id,
                chat_id=chat_id,
                customer_number=customer_number,
                business_name=business_name,
                objective=objective,
                session_manager=session_manager,
            ),
            name=f"vapi-summary:{call_id}",
        )
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)

    async def _watch_call(
        self,
        *,
        call_id: str,
        chat_id: str,
        customer_number: str,
        business_name: str,
        objective: str,
        session_manager: Any = None,
    ) -> None:
        api_key = self._config.api_key
        if api_key is None or not self._telegram_bot_token:
            return

        deadline = asyncio.get_running_loop().time() + POLL_TIMEOUT_SECONDS
        headers = {"Authorization": f"Bearer {api_key.get_secret_value()}"}
        call_url = f"{self._config.base_url.rstrip('/')}/call/{call_id}"
        analysis_waits = 0
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                while asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(POLL_INTERVAL_SECONDS)
                    try:
                        response = await client.get(call_url, headers=headers)
                        response.raise_for_status()
                        call = response.json()
                    except (httpx.HTTPError, ValueError):
                        logger.warning(
                            "vapi_summary_poll_failed",
                            extra={"vapi.call_id": call_id},
                            exc_info=True,
                        )
                        continue
                    if not isinstance(call, dict) or call.get("status") != "ended":
                        continue
                    if session_manager is not None:
                        session_manager.update_operation_status(
                            call_id,
                            "ended",
                            metadata={
                                "ended_reason": call.get("endedReason"),
                            },
                        )
                    raw_analysis = call.get("analysis")
                    analysis: dict[str, Any] = (
                        raw_analysis if isinstance(raw_analysis, dict) else {}
                    )
                    raw_structured = analysis.get("structuredData")
                    structured: dict[str, Any] = (
                        raw_structured if isinstance(raw_structured, dict) else {}
                    )
                    analysis_ready = bool(
                        str(
                            analysis.get("summary") or structured.get("summary") or ""
                        ).strip()
                    )
                    if not analysis_ready and analysis_waits < 6:
                        analysis_waits += 1
                        continue
                    await self._send_telegram_summary(
                        chat_id,
                        _render_call_summary(
                            call,
                            call_id=call_id,
                            customer_number=customer_number,
                            business_name=business_name,
                            objective=objective,
                        ),
                    )
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "vapi_summary_watcher_failed", extra={"vapi.call_id": call_id}
            )
            return

        await self._send_telegram_summary(
            chat_id,
            f"Call update: {business_name or customer_number}\n\n"
            "I couldn't retrieve the final call summary within 15 minutes. "
            f"Call ID: {call_id}",
        )

    async def _send_telegram_summary(self, chat_id: str, text: str) -> None:
        assert self._telegram_bot_token is not None
        url = f"https://api.telegram.org/bot{self._telegram_bot_token}/sendMessage"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, json={"chat_id": chat_id, "text": text})
            response.raise_for_status()


class VapiCallStatusTool(Tool):
    """Retrieve status for a durable Vapi call operation without placing a call."""

    def __init__(self, config: VapiConfig) -> None:
        self._config = config

    @property
    def name(self) -> str:
        return "vapi_call_status"

    @property
    def description(self) -> str:
        return (
            "Get the current or final status of an existing Vapi call. Use this for "
            "follow-ups such as 'what happened?' and never place another call to "
            "answer a status question."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "call_id": {
                    "type": "string",
                    "description": (
                        "Optional Vapi call ID. Omit to inspect the newest call "
                        "recorded for this conversation."
                    ),
                }
            },
        }

    async def execute(
        self, input_data: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        if context.provider != "telegram":
            return ToolResult.error(
                "Vapi call status is restricted to Telegram-dispatched tasks"
            )
        if not self._config.enabled:
            return ToolResult.error("Vapi is disabled")
        api_key = self._config.api_key
        if api_key is None:
            return ToolResult.error("VAPI_API_KEY is not configured")

        call_id = str(input_data.get("call_id") or "").strip()
        session_manager = context.session_manager
        if session_manager is None:
            return ToolResult.error(
                "No durable conversation record is available for call status"
            )

        if call_id:
            operation = session_manager.get_operation(call_id)
            if operation is None or operation.kind != "vapi_call":
                return ToolResult.error(
                    "That call is not recorded in this conversation"
                )
        else:
            operation = session_manager.latest_operation("vapi_call")
            if operation is None:
                return ToolResult.error("No Vapi call was found for this conversation")
            call_id = operation.operation_id

        headers = {"Authorization": f"Bearer {api_key.get_secret_value()}"}
        base_url = self._config.base_url.rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    f"{base_url}/call/{call_id}", headers=headers
                )
                response.raise_for_status()
                call = response.json()
        except httpx.HTTPStatusError as exc:
            return ToolResult.error(
                f"Vapi rejected status lookup ({exc.response.status_code}): "
                f"{exc.response.text[:500]}"
            )
        except (httpx.HTTPError, ValueError) as exc:
            return ToolResult.error(f"Vapi call status lookup failed: {exc}")

        if not _belongs_to_configured_assistant(call, self._config):
            return ToolResult.error(
                "That call does not belong to the configured Vapi assistant"
            )

        analysis = call.get("analysis")
        analysis_data = analysis if isinstance(analysis, dict) else {}
        summary = str(analysis_data.get("summary") or "").strip() or None
        status = str(call.get("status") or "unknown")
        session_manager.update_operation_status(
            call_id,
            status,
            metadata={"ended_reason": call.get("endedReason")},
        )

        return ToolResult.success(
            json.dumps(
                {
                    "call_id": call_id,
                    "status": status,
                    "ended_reason": call.get("endedReason"),
                    "summary": summary,
                },
                indent=2,
            )
        )


class VapiEndCallTool(Tool):
    """Immediately terminate the latest active configured Vapi call."""

    def __init__(self, config: VapiConfig) -> None:
        self._config = config

    @property
    def name(self) -> str:
        return "vapi_end_call"

    @property
    def description(self) -> str:
        return (
            "Immediately end an active Vapi outbound call when the Telegram user "
            "explicitly asks to stop, cancel, hang up, or end the call."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "call_id": {
                    "type": "string",
                    "description": (
                        "Optional Vapi call ID. Omit to stop the newest active call "
                        "recorded for this conversation."
                    ),
                }
            },
        }

    async def execute(
        self, input_data: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        if context.provider != "telegram":
            return ToolResult.error(
                "Vapi call termination is restricted to Telegram-dispatched tasks"
            )
        if not self._config.enabled:
            return ToolResult.error("Vapi is disabled")
        api_key = self._config.api_key
        if api_key is None:
            return ToolResult.error("VAPI_API_KEY is not configured")

        requested_id = str(input_data.get("call_id") or "").strip()
        session_manager = context.session_manager
        if session_manager is None:
            return ToolResult.error(
                "No durable conversation record is available for call termination"
            )
        if requested_id:
            operation = session_manager.get_operation(requested_id)
            if operation is None or operation.kind != "vapi_call":
                return ToolResult.error(
                    "That call is not recorded in this conversation"
                )
        else:
            operation = session_manager.latest_operation("vapi_call")
            if operation is None:
                return ToolResult.error(
                    "There is no outbound call recorded for this conversation"
                )
            requested_id = operation.operation_id

        headers = {"Authorization": f"Bearer {api_key.get_secret_value()}"}
        base_url = self._config.base_url.rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    f"{base_url}/call/{requested_id}", headers=headers
                )
                response.raise_for_status()
                call = response.json()
                if not _is_configured_call(call, self._config):
                    return ToolResult.error(
                        "There is no active outbound call to end in this conversation"
                    )

                control_url = _control_url(call)
                if control_url is None:
                    call_id = str(call.get("id") or "")
                    response = await client.get(
                        f"{base_url}/call/{call_id}", headers=headers
                    )
                    response.raise_for_status()
                    call = response.json()
                    control_url = _control_url(call)
                if control_url is None:
                    return ToolResult.error(
                        "Vapi did not provide a valid control URL for the active call"
                    )

                response = await client.post(control_url, json={"type": "end-call"})
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return ToolResult.error(
                f"Vapi rejected call termination ({exc.response.status_code}): "
                f"{exc.response.text[:500]}"
            )
        except (httpx.HTTPError, ValueError) as exc:
            return ToolResult.error(f"Vapi call termination failed: {exc}")

        if call.get("id"):
            session_manager.update_operation_status(
                str(call.get("id")), "termination_requested"
            )
        return ToolResult.success(
            json.dumps(
                {
                    "call_id": call.get("id"),
                    "status": "termination_requested",
                },
                indent=2,
            )
        )


def _active_call_to_number(
    calls: Any, config: VapiConfig, customer_number: str
) -> dict[str, Any] | None:
    if not isinstance(calls, list):
        return None
    matching = []
    for call in calls:
        if not _is_configured_call(call, config):
            continue
        customer = call.get("customer")
        if not isinstance(customer, dict) or customer.get("number") != customer_number:
            continue
        matching.append(call)
    if not matching:
        return None
    return max(matching, key=lambda call: str(call.get("createdAt") or ""))


def _is_configured_call(call: Any, config: VapiConfig) -> bool:
    return (
        _belongs_to_configured_assistant(call, config)
        and call.get("status") in ACTIVE_CALL_STATUSES
    )


def _belongs_to_configured_assistant(call: Any, config: VapiConfig) -> bool:
    return (
        isinstance(call, dict)
        and call.get("assistantId") == config.assistant_id
        and call.get("phoneNumberId") == config.phone_number_id
    )


def _control_url(call: Any) -> str | None:
    if not isinstance(call, dict):
        return None
    monitor = call.get("monitor")
    if not isinstance(monitor, dict):
        return None
    value = str(monitor.get("controlUrl") or "").strip()
    if not value:
        return None
    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (
        hostname == "vapi.ai" or hostname.endswith(".vapi.ai")
    ):
        return None
    return value


def _clean_voice_text(value: Any, *, limit: int) -> str:
    text = str(value or "")
    text = text.translate(
        str.maketrans(
            {
                "\x19": "'",
                "\u2018": "'",
                "\u2019": "'",
                "\u201c": '"',
                "\u201d": '"',
                "\u2013": "-",
                "\u2014": "-",
            }
        )
    )
    text = CONTROL_CHAR_RE.sub(" ", text)
    return " ".join(text.split())[:limit].strip()


def _render_call_summary(
    call: dict[str, Any],
    *,
    call_id: str,
    customer_number: str,
    business_name: str,
    objective: str,
) -> str:
    raw_analysis = call.get("analysis")
    analysis: dict[str, Any] = raw_analysis if isinstance(raw_analysis, dict) else {}
    raw_artifact = call.get("artifact")
    artifact: dict[str, Any] = raw_artifact if isinstance(raw_artifact, dict) else {}
    raw_structured = analysis.get("structuredData")
    structured: dict[str, Any] = (
        raw_structured if isinstance(raw_structured, dict) else {}
    )
    summary = str(analysis.get("summary") or structured.get("summary") or "").strip()
    actions = structured.get("actionItems") or structured.get("action_items") or []
    if isinstance(actions, str):
        actions = [actions]
    if not isinstance(actions, list):
        actions = []
    action_text = "; ".join(str(item).strip() for item in actions if str(item).strip())
    transcript = str(call.get("transcript") or artifact.get("transcript") or "").strip()
    if not summary:
        summary = (
            transcript[:1200]
            if transcript
            else "No transcript or summary was available."
        )
    if not action_text:
        action_text = "None identified."

    target = business_name or customer_number
    ended_reason = str(call.get("endedReason") or "unknown")
    return "\n".join(
        [
            f"Call complete: {target}",
            f"Outcome: {ended_reason}",
            "",
            f"Summary: {summary}",
            "",
            f"Action needed: {action_text}",
            "",
            f"Objective: {objective}",
            f"Call ID: {call_id}",
        ]
    )
