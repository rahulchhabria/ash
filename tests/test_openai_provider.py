"""Tests for OpenAI LLM provider."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import openai

from ash.llm.openai import OpenAIProvider
from ash.llm.types import Message, Role


class TestOpenAIBuildRequestKwargs:
    """Tests for OpenAI provider request building."""

    def setup_method(self):
        self.provider = OpenAIProvider(api_key="test-key")

    def test_reasoning_included_when_set(self):
        """Test that reasoning effort is passed to API kwargs."""
        messages = [Message(role=Role.USER, content="Hello")]
        kwargs = self.provider._build_request_kwargs(
            messages=messages,
            model="gpt-5.2-pro",
            tools=None,
            system=None,
            max_tokens=4096,
            temperature=None,
            reasoning="high",
        )
        assert kwargs["reasoning"] == {"effort": "high"}

    def test_reasoning_not_included_when_none(self):
        """Test that reasoning is omitted when not set."""
        messages = [Message(role=Role.USER, content="Hello")]
        kwargs = self.provider._build_request_kwargs(
            messages=messages,
            model="gpt-5.2",
            tools=None,
            system=None,
            max_tokens=4096,
            temperature=0.7,
        )
        assert "reasoning" not in kwargs

    def test_reasoning_medium(self):
        """Test medium reasoning effort value."""
        messages = [Message(role=Role.USER, content="Hello")]
        kwargs = self.provider._build_request_kwargs(
            messages=messages,
            model="gpt-5.2-pro",
            tools=None,
            system=None,
            max_tokens=4096,
            temperature=None,
            reasoning="medium",
        )
        assert kwargs["reasoning"] == {"effort": "medium"}

    def test_reasoning_low(self):
        """Test low reasoning effort value."""
        messages = [Message(role=Role.USER, content="Hello")]
        kwargs = self.provider._build_request_kwargs(
            messages=messages,
            model="gpt-5.2",
            tools=None,
            system=None,
            max_tokens=4096,
            temperature=None,
            reasoning="low",
        )
        assert kwargs["reasoning"] == {"effort": "low"}

    def test_temperature_omitted_for_gpt5_models(self):
        """GPT-5 reasoning models reject custom temperature values."""
        messages = [Message(role=Role.USER, content="Hello")]
        kwargs = self.provider._build_request_kwargs(
            messages=messages,
            model="gpt-5.2",
            tools=None,
            system=None,
            max_tokens=4096,
            temperature=0.7,
        )
        assert "temperature" not in kwargs

    def test_temperature_included_for_non_gpt5_models(self):
        """Non GPT-5 models can still receive custom temperature values."""
        messages = [Message(role=Role.USER, content="Hello")]
        kwargs = self.provider._build_request_kwargs(
            messages=messages,
            model="gpt-4o",
            tools=None,
            system=None,
            max_tokens=4096,
            temperature=0.7,
        )
        assert kwargs["temperature"] == 0.7

    async def test_complete_retries_configured_model_fallback_on_not_found(self):
        """Known unavailable model aliases should retry with the fallback model."""
        messages = [Message(role=Role.USER, content="Hello")]
        request = httpx.Request("POST", "https://api.openai.test/v1/responses")
        not_found = openai.NotFoundError(
            "model not found",
            response=httpx.Response(404, request=request),
            body={"error": {"code": "model_not_found"}},
        )
        response = SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="message",
                    content=[SimpleNamespace(type="output_text", text="hi")],
                )
            ],
            usage=None,
            model="gpt-5.2",
            model_dump=lambda: {"model": "gpt-5.2"},
        )
        self.provider._client.responses.create = AsyncMock(
            side_effect=[not_found, response]
        )

        result = await self.provider.complete(messages, model="gpt-5.6")

        assert result.model == "gpt-5.2"
        calls = self.provider._client.responses.create.await_args_list
        assert calls[0].kwargs["model"] == "gpt-5.6"
        assert calls[1].kwargs["model"] == "gpt-5.2"
