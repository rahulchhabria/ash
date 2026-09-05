"""Vapi outbound calling tool."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

import httpx

from ash.config.models import VapiConfig
from ash.tools.base import Tool, ToolContext, ToolResult

E164_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")
POLL_INTERVAL_SECONDS = 5
POLL_TIMEOUT_SECONDS = 15 * 60

logger = logging.getLogger(__name__)
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()
ACTIVE_CALL_STATUSES = {"queued", "ringing", "in-progress", "forwarding"}


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
                    "description": "Optional name for the called party or business.",
                },
                "approved": {
                    "type": "boolean",
                    "description": (
                        "Set true only after the user approves the exact call at an "
                        "interrupt checkpoint."
                    ),
                },
            },
            "required": ["customer_number", "objective", "approved"],
        }

    async def execute(
        self, input_data: dict[str, Any], context: ToolContext
    ) -> ToolResult:
        if context.provider != "telegram":
            return ToolResult.error(
                "Outbound Vapi calls are restricted to Telegram-dispatched tasks"
            )
        if input_data.get("approved") is not True:
            return ToolResult.error(
                "Explicit Telegram approval is required before placing a call"
            )
        if not self._config.enabled:
            return ToolResult.error("Vapi is disabled")

        number = str(input_data.get("customer_number") or "").strip()
        if not E164_RE.fullmatch(number):
            return ToolResult.error("customer_number must use E.164 format")
        objective = str(input_data.get("objective") or "").strip()
        if not objective:
            return ToolResult.error("objective is required")
        if len(objective) > 2000:
            return ToolResult.error("objective must be 2000 characters or fewer")

        variables = {
            "objective": objective,
            "ash_objective": objective,
            "ash_business_name": str(input_data.get("business_name") or "")[:300],
            "ash_context": str(input_data.get("context") or "")[:4000],
            "ash_customer_name": str(input_data.get("customer_name") or "")[:300],
        }
        if self._config.dry_run:
            summary = {
                "call_id": None,
                "status": "dry_run",
                "customer_number": number,
                "business_name": variables["ash_business_name"] or None,
                "objective": objective,
            }
            return ToolResult.success(json.dumps(summary, indent=2))

        api_key = self._config.api_key
        if api_key is None:
            return ToolResult.error("VAPI_API_KEY is not configured")
        if not self._config.assistant_id or not self._config.phone_number_id:
            return ToolResult.error(
                "Vapi outbound calling requires assistant_id and phone_number_id"
            )

        payload = {
            "assistantId": self._config.assistant_id,
            "phoneNumberId": self._config.phone_number_id,
            "customer": {"number": number},
            "assistantOverrides": {"variableValues": variables},
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{self._config.base_url.rstrip('/')}/call",
                    headers={
                        "Authorization": f"Bearer {api_key.get_secret_value()}",
                        "Content-Type": "application/json",
                    },
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
            self._start_summary_watcher(
                call_id=call_id,
                chat_id=context.chat_id or self._config.telegram_chat_id,
                customer_number=number,
                business_name=variables["ash_business_name"],
                objective=objective,
            )
        summary = {
            "call_id": call_id or None,
            "status": result.get("status") or "queued",
            "business_name": variables["ash_business_name"] or None,
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
                    analysis = (
                        call.get("analysis")
                        if isinstance(call.get("analysis"), dict)
                        else {}
                    )
                    structured = (
                        analysis.get("structuredData")
                        if isinstance(analysis.get("structuredData"), dict)
                        else {}
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
                        "for the configured assistant and phone number."
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
        headers = {"Authorization": f"Bearer {api_key.get_secret_value()}"}
        base_url = self._config.base_url.rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                if requested_id:
                    response = await client.get(
                        f"{base_url}/call/{requested_id}", headers=headers
                    )
                    response.raise_for_status()
                    call = response.json()
                    if not _is_configured_call(call, self._config):
                        return ToolResult.error(
                            "That call does not belong to the configured Vapi assistant"
                        )
                else:
                    response = await client.get(
                        f"{base_url}/call", headers=headers, params={"limit": 20}
                    )
                    response.raise_for_status()
                    calls = response.json()
                    call = _latest_active_call(calls, self._config)
                    if call is None:
                        return ToolResult.error(
                            "There is no active outbound call to end"
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

        return ToolResult.success(
            json.dumps(
                {
                    "call_id": call.get("id"),
                    "status": "termination_requested",
                },
                indent=2,
            )
        )


def _latest_active_call(calls: Any, config: VapiConfig) -> dict[str, Any] | None:
    if not isinstance(calls, list):
        return None
    matching = [
        call
        for call in calls
        if isinstance(call, dict)
        and call.get("status") in ACTIVE_CALL_STATUSES
        and _is_configured_call(call, config)
    ]
    if not matching:
        return None
    return max(matching, key=lambda call: str(call.get("createdAt") or ""))


def _is_configured_call(call: Any, config: VapiConfig) -> bool:
    return (
        isinstance(call, dict)
        and call.get("status") in ACTIVE_CALL_STATUSES
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


def _render_call_summary(
    call: dict[str, Any],
    *,
    call_id: str,
    customer_number: str,
    business_name: str,
    objective: str,
) -> str:
    analysis = call.get("analysis") if isinstance(call.get("analysis"), dict) else {}
    artifact = call.get("artifact") if isinstance(call.get("artifact"), dict) else {}
    structured = (
        analysis.get("structuredData")
        if isinstance(analysis.get("structuredData"), dict)
        else {}
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
