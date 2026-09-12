"""Tests for OpenAI LLM provider."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import openai
import pytest

from ash.llm.openai import OpenAIProvider
from ash.llm.types import Message, Role, StreamEventType


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

    def test_hosted_tools_disabled_for_custom_compatible_endpoint(self):
        custom = OpenAIProvider(api_key="x", base_url="https://llm.example/v1")
        official = OpenAIProvider(api_key="x", base_url="https://api.openai.com/v1/")
        assert not custom.supports_hosted_openai_tools
        assert official.supports_hosted_openai_tools

    def test_parse_response_preserves_safe_url_citations(self):
        response = SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="message",
                    content=[
                        SimpleNamespace(
                            type="output_text",
                            text="answer",
                            annotations=[
                                SimpleNamespace(
                                    type="url_citation",
                                    title="Source] one",
                                    url="https://example.com/a(b)",
                                ),
                                SimpleNamespace(
                                    type="url_citation",
                                    title="bad",
                                    url="javascript:alert(1)",
                                ),
                                SimpleNamespace(
                                    type="url_citation",
                                    title="forged\n- bullet",
                                    url="https://evil.example/x\n- forged",
                                ),
                            ],
                        )
                    ],
                )
            ],
            usage=None,
            model="gpt-5.2",
            model_dump=lambda: {},
        )
        parsed = self.provider._parse_response(response)
        text = parsed.message.get_text()
        assert "[Source\\] one](https://example.com/a%28b%29)" in text
        assert "javascript:" not in text
        assert "evil.example" not in text

    @pytest.mark.asyncio
    async def test_stream_emits_deduplicated_citations_before_end(self):
        citation = SimpleNamespace(
            type="url_citation", title="Source", url="https://example.com/a"
        )

        async def events():
            yield SimpleNamespace(type="response.output_text.delta", delta="answer")
            yield SimpleNamespace(
                type="response.output_text.annotation.added", annotation=citation
            )
            yield SimpleNamespace(
                type="response.output_text.annotation.added", annotation=citation
            )
            yield SimpleNamespace(type="response.completed")

        self.provider._client.responses.create = AsyncMock(return_value=events())
        chunks = [
            chunk
            async for chunk in self.provider.stream(
                [Message(role=Role.USER, content="question")]
            )
        ]
        joined = "".join(chunk.content or "" for chunk in chunks)
        assert joined.count("https://example.com/a") == 1
        assert chunks[-2].type == StreamEventType.TEXT_DELTA
        assert chunks[-1].type == StreamEventType.MESSAGE_END

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
