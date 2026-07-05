"""Pioneer LLM provider using Pioneer's OpenAI-compatible endpoint."""

from ash.llm.openai import OpenAIProvider

PIONEER_BASE_URL = "https://api.pioneer.ai/v1"


class PioneerProvider(OpenAIProvider):
    """Pioneer provider using the OpenAI-compatible Responses API."""

    def __init__(self, api_key: str | None = None):
        headers = {"X-API-Key": api_key} if api_key else None
        super().__init__(
            api_key=api_key or "pioneer",
            base_url=PIONEER_BASE_URL,
            default_headers=headers,
            provider_name="pioneer",
        )
