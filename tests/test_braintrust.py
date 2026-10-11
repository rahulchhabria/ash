from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from pydantic import SecretStr

from ash.config import AshConfig
from ash.config.loader import _resolve_env_secrets
from ash.config.models import BraintrustConfig
from ash.core.session import SessionState
from ash.integrations.braintrust import BraintrustIntegration
from ash.integrations.runtime import IntegrationContext, IntegrationRuntime
from ash.llm.types import ToolUse
from ash.observability.braintrust import conversation_record, scrub
from evals.braintrust_export import BraintrustExporter, result_record
from evals.runner import EvalResult
from evals.types import EvalCase, JudgeResult


def context(*, enabled: bool = True, key: bool = True) -> IntegrationContext:
    config = AshConfig.model_validate(
        {
            "models": {"default": {"provider": "openai", "model": "test"}},
            "braintrust": {"enabled": enabled, "api_key": "test-key" if key else None},
        }
    )
    return IntegrationContext(config=config, components=cast(Any, None), mode="serve")


def conversation() -> SessionState:
    session = SessionState("session-one", "telegram", "chat", "user")
    session.add_user_message("old request")
    session.add_assistant_message("old answer")
    session.add_user_message("create reminder")
    session.add_assistant_message([ToolUse("call1", "schedule", {"api_key": "secret"})])
    session.add_tool_result("call1", "failed", is_error=True)
    session.add_assistant_message("Unable to create reminder")
    return session


def test_content_opt_in_and_no_invented_accuracy() -> None:
    record = conversation_record(
        "create reminder", conversation(), BraintrustConfig(), {}
    )
    assert "input" not in record and "output" not in record
    assert "scores" not in record
    assert record["metrics"] == {"tool_calls": 1, "tool_errors": 1}
    assert record["metadata"]["conversation_id"] == "session-one"


def test_capture_scrubs_tool_credentials_and_marks_truncation() -> None:
    record = conversation_record(
        "create reminder",
        conversation(),
        BraintrustConfig(capture_content=True, max_messages=3),
        {},
    )
    assert record["metadata"]["history_truncated"]
    assert '"secret"' not in json.dumps(record)
    assert record["metrics"]["tool_errors"] == 1
    assert len(record["output"]["retained_messages"]) == 3


def test_scrub_nested_free_text_before_truncation() -> None:
    clean = scrub(
        {
            "Authorization": "Bearer abc",
            "nested": [{"BRAINTRUST_API_KEY": "private"}],
            "text": "a@example.com TOKEN=very-secret https://api.telegram.org/bot123:xyz/method",
        }
    )
    serialized = json.dumps(clean)
    for secret in ("abc", "private", "a@example.com", "very-secret", "123:xyz"):
        assert secret not in serialized
    assert scrub("x" * 500, 100).endswith("[truncated]")


def test_missing_boundary_does_not_attribute_old_tools() -> None:
    record = conversation_record(
        "compacted away", conversation(), BraintrustConfig(), {}
    )
    assert not record["metadata"]["turn_boundary_found"]
    assert record["metrics"]["tool_calls"] == 0


def test_key_resolves_only_in_explicit_section(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAINTRUST_API_KEY", "local-secret")
    assert "braintrust" not in _resolve_env_secrets({})
    settings = _resolve_env_secrets({"braintrust": {"enabled": True}})
    assert isinstance(settings["braintrust"]["api_key"], SecretStr)
    assert "local-secret" not in repr(settings)


@pytest.mark.asyncio
async def test_disabled_does_not_import_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib, "import_module", Mock(side_effect=AssertionError))
    integration = BraintrustIntegration()
    await integration.setup(context(enabled=False))
    await integration.on_message_postprocess("hello", conversation(), "user", context())
    await integration.on_shutdown(context())


@pytest.mark.asyncio
async def test_missing_key_isolated_as_failed_integration() -> None:
    runtime = IntegrationRuntime([BraintrustIntegration()])
    await runtime.setup(context(key=False))
    assert runtime.health_snapshot().failed_setup == ("braintrust",)


@pytest.mark.asyncio
async def test_export_failure_does_not_break_following_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Mock()
    client.log.side_effect = [RuntimeError("offline"), None]
    init_logger = Mock(return_value=client)
    monkeypatch.setitem(
        sys.modules, "braintrust", SimpleNamespace(init_logger=init_logger)
    )
    integration = BraintrustIntegration()
    ctx = context()
    await integration.setup(ctx)
    for _ in range(2):
        await integration.on_message_postprocess(
            "create reminder", conversation(), "u", ctx
        )
    await integration.on_shutdown(ctx)
    assert client.log.call_count == 2
    assert client.flush.call_count == 1
    assert init_logger.call_args.kwargs["set_current"] is False
    assert integration._worker is None


@pytest.mark.asyncio
async def test_queue_overflow_is_nonblocking(caplog: pytest.LogCaptureFixture) -> None:
    integration = BraintrustIntegration()
    integration._queue = asyncio.Queue(maxsize=1)
    ctx = context()
    for _ in range(2):
        await integration.on_message_postprocess(
            "create reminder", conversation(), "u", ctx
        )
    assert integration._queue.qsize() == 1
    assert "braintrust_queue_full" in caplog.text


def eval_result(*, judge_error: bool = False) -> EvalResult:
    return EvalResult(
        case=EvalCase(id="reminder", prompt="remind me", expected_behavior="create it"),
        response_text="Done",
        tool_calls=[],
        judge_result=JudgeResult(
            passed=False,
            score=0,
            reasoning="failed",
            judge_error=judge_error,
        ),
    )


def test_judge_error_is_not_a_quality_score() -> None:
    result = eval_result(judge_error=True)
    result.judge_result.passed = True  # An inconsistent judge must still fail closed.
    record = result_record("scheduler", result)
    assert "quality" not in record["scores"]
    assert record["scores"]["passed"] == 0
    assert record["metadata"]["judge_error"]
    assert result_record("scheduler", eval_result())["scores"]["quality"] == 0


def test_experiment_export_and_flush(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAINTRUST_API_KEY", "local-key")
    experiment = Mock()
    init = Mock(return_value=experiment)
    monkeypatch.setitem(sys.modules, "braintrust", SimpleNamespace(init=init))
    exporter = BraintrustExporter("ash", "candidate")
    exporter.log("scheduler", eval_result())
    exporter.finish()
    init.assert_called_once_with(
        project="ash", experiment="candidate", set_current=False
    )
    assert experiment.log.call_args.kwargs["metadata"]["case_id"] == "reminder"
    experiment.flush.assert_called_once()


def test_architecture_keeps_braintrust_out_of_core_and_providers() -> None:
    root = Path(__file__).parents[1] / "src/ash"
    for directory in (root / "core", root / "providers"):
        for source in directory.rglob("*.py"):
            assert "braintrust" not in source.read_text(), str(source)
