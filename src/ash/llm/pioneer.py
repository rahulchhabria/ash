"""Pioneer LLM provider using Pioneer's OpenAI-compatible endpoint."""

from ash.llm.openai import OpenAIProvider

PIONEER_BASE_URL = "https://api.pioneer.ai/v1"


class PioneerProvider(OpenAIProvider):
    """Pioneer provider using the OpenAI-compatible Responses API."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        default_headers: dict[str, str] | None = None,
        provider_name: str = "pioneer",
    ):
        headers = default_headers
        if headers is None and api_key:
            headers = {"X-API-Key": api_key}
        super().__init__(
            api_key=api_key or "pioneer",
            base_url=base_url or PIONEER_BASE_URL,
            default_headers=headers,
            provider_name=provider_name,
        )
