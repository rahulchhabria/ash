"""Tracing adapters. Spec: specs/integrations.md (Tracing)."""

from __future__ import annotations

import json
from contextlib import ExitStack
from typing import Any

from ash.observability.braintrust import scrub


class SentryObserver:
    def __init__(self, span: Any = None, stack: ExitStack | None = None):
        self.span = span
        self.stack = stack

    def start(self, kind: str, name: str, data: dict[str, Any]) -> SentryObserver:
        import sentry_sdk
        from sentry_sdk.ai import set_conversation_id

        # Provider SDK instrumentation already creates the LLM child spans.
        if kind == "llm":
            return SentryObserver()
        stack = ExitStack()
        try:
            if kind == "agent":
                stack.enter_context(sentry_sdk.isolation_scope())
                set_conversation_id(data["conversation_id"])
                span = stack.enter_context(
                    sentry_sdk.start_transaction(name=name, op="gen_ai.invoke_agent")
                )
                span.set_data("gen_ai.agent.name", "ash")
                span.set_data("gen_ai.conversation.id", data["conversation_id"])
            else:
                span = stack.enter_context(
                    sentry_sdk.start_span(name=name, op="gen_ai.execute_tool")
                )
                span.set_data("gen_ai.tool.name", name)
            span.set_data(
                "gen_ai.operation.name",
                "invoke_agent" if kind == "agent" else "execute_tool",
            )
            return SentryObserver(span, stack)
        except BaseException:
            stack.close()
            raise

    def result(self, result: Any) -> None:
        if (
            self.span is not None
            and isinstance(result, dict)
            and result.get("is_error")
        ):
            self.span.set_status("internal_error")

    def chunk(self, chunk: Any) -> None:
        pass

    def usage(self, *args: Any) -> None:
        pass

    def finish(self, error: str | None) -> None:
        try:
            if error and self.span is not None:
                self.span.set_status(
                    "cancelled"
                    if error in ("CancelledError", "GeneratorExit")
                    else "internal_error"
                )
        finally:
            if self.stack:
                self.stack.close()


class BraintrustObserver:
    def __init__(
        self, parent: Any, config: Any, metadata: dict[str, Any], kind: str = ""
    ):
        self.parent = parent
        self.config = config
        self.metadata = metadata
        self.kind = kind
        self.chunks: list[Any] = []
        self.size = 0
        self.truncated = False
        self.text = ""
        self.tools: dict[str, dict[str, Any]] = {}

    def start(self, kind: str, name: str, data: dict[str, Any]) -> BraintrustObserver:
        metadata = {**self.metadata, **{k: v for k, v in data.items() if k != "input"}}
        from ash.observability import SENTRY_AVAILABLE

        current = None
        if SENTRY_AVAILABLE:
            import sentry_sdk

            current = sentry_sdk.get_current_span()
        if current:
            metadata["sentry_trace_id"] = current.trace_id
        event: dict[str, Any] = {
            "metadata": scrub(metadata, self.config.max_content_chars)
        }
        if self.config.capture_content:
            value = data.get("input")
            if kind == "llm" and isinstance(value, dict):
                value = (
                    [{"role": "system", "content": value["system"]}]
                    if value.get("system")
                    else []
                ) + value.get("messages", [])
            event["input"] = self._content(value)
        span = self.parent.start_span(
            name=name,
            type="llm" if kind == "llm" else "tool" if kind == "tool" else "task",
            set_current=False,
            **event,
        )
        return BraintrustObserver(span, self.config, metadata, kind)

    def _content(self, value: Any) -> Any:
        # Bound the entire payload as well as individual strings.
        clean = scrub(value, self.config.max_content_chars)
        if (
            len(json.dumps(clean, default=str))
            > self.config.max_content_chars * self.config.max_messages
        ):
            return {"truncated": True}
        return clean

    def result(self, result: Any) -> None:
        if not isinstance(result, dict):
            return
        if self.kind == "llm":
            usage = result.get("usage")
            if usage:
                self.usage(
                    usage["input_tokens"], usage["output_tokens"], result.get("model")
                )
            output = [result.get("message")]
        else:
            output = result.get("text", result.get("content"))
        if result.get("is_error"):
            self.parent.log(error="Tool returned an error")
        if self.config.capture_content:
            self.parent.log(output=self._content(output))

    def chunk(self, chunk: Any) -> None:
        if not self.config.capture_content:
            return
        clean = chunk
        size = len(json.dumps(clean, default=str))
        if self.size + size <= self.config.max_content_chars * self.config.max_messages:
            self.chunks.append(clean)
            self.size += size
            if isinstance(clean, str):
                self.text += clean
            elif isinstance(clean, dict):
                if clean.get("type") == "text_delta":
                    self.text += clean.get("content") or ""
                elif clean.get("type") == "tool_use_start":
                    self.tools[clean["tool_use_id"]] = {
                        "id": clean["tool_use_id"],
                        "type": "function",
                        "function": {"name": clean.get("tool_name"), "arguments": ""},
                    }
                elif (
                    clean.get("type") in ("tool_use_delta", "tool_use_end")
                    and clean.get("tool_use_id") in self.tools
                ):
                    content = clean.get("content")
                    if isinstance(content, str):
                        if clean["type"] == "tool_use_end":
                            self.tools[clean["tool_use_id"]]["function"][
                                "arguments"
                            ] = content
                        else:
                            self.tools[clean["tool_use_id"]]["function"][
                                "arguments"
                            ] += content
        else:
            self.truncated = True

    def usage(
        self, input_tokens: int, output_tokens: int, model: str | None = None
    ) -> None:
        event: dict[str, Any] = {
            "metrics": {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "tokens": input_tokens + output_tokens,
            }
        }
        if model:
            event["metadata"] = {"model": model}
        self.parent.log(**event)

    def finish(self, error: str | None) -> None:
        try:
            if self.chunks:
                output: Any = (
                    [
                        {
                            "role": "assistant",
                            "content": self.text,
                            "tool_calls": list(self.tools.values()),
                        }
                    ]
                    if self.kind == "llm"
                    else self.text
                )
                self.parent.log(
                    output=self._content(output),
                    metadata={"output_truncated": self.truncated},
                )
            if error:
                self.parent.log(error=error)
        finally:
            self.parent.end()
