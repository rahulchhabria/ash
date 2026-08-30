"""Vapi outbound calling tool."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from ash.config.models import VapiConfig
from ash.tools.base import Tool, ToolContext, ToolResult

E164_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")


class VapiOutboundCallTool(Tool):
    """Place one explicitly approved outbound call through Vapi."""

    def __init__(self, config: VapiConfig) -> None:
        self._config = config

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
        api_key = self._config.api_key
        if api_key is None:
            return ToolResult.error("VAPI_API_KEY is not configured")
        if not self._config.assistant_id or not self._config.phone_number_id:
            return ToolResult.error(
                "Vapi outbound calling requires assistant_id and phone_number_id"
            )

        number = str(input_data.get("customer_number") or "").strip()
        if not E164_RE.fullmatch(number):
            return ToolResult.error("customer_number must use E.164 format")
        objective = str(input_data.get("objective") or "").strip()
        if not objective:
            return ToolResult.error("objective is required")
        if len(objective) > 2000:
            return ToolResult.error("objective must be 2000 characters or fewer")

        variables = {
            "ash_objective": objective,
            "ash_business_name": str(input_data.get("business_name") or "")[:300],
            "ash_context": str(input_data.get("context") or "")[:4000],
            "ash_customer_name": str(input_data.get("customer_name") or "")[:300],
        }
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
        summary = {
            "call_id": result.get("id"),
            "status": result.get("status") or "queued",
            "business_name": variables["ash_business_name"] or None,
        }
        return ToolResult.success(json.dumps(summary, indent=2))
