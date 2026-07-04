"""Tests for the DeepAgents-backed research pipeline."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

from ash.agents.builtin.research import ResearchAgent
from ash.agents.types import AgentContext
from ash.llm.types import CompletionResponse, Message, Role
from ash.research.service import (
    CodexResearchReviewer,
    CodexReviewResult,
    DeepAgentsRunResult,
    EmailDeliveryResult,
    LocalResearchMailer,
    ResearchJobPaths,
    ResearchRequest,
    ResearchResult,
    ResearchService,
    create_research_job_paths,
)


class _FakeBackend:
    def __init__(self, result: DeepAgentsRunResult) -> None:
        self._result = result

    async def run(self, request: ResearchRequest, paths) -> DeepAgentsRunResult:  # noqa: ANN001
        return self._result


class _FakeExtractor:
    async def extract(self, text: str, labels: dict[str, str]) -> dict:
        return {
            "available": True,
            "labels": labels,
            "entities": {
                "organization": [{"text": "OpenAI", "score": 0.91}],
                "product": [{"text": "Codex", "score": 0.88}],
            },
        }


class _FakeReviewer:
    async def review(
        self,
        request: ResearchRequest,
        report_text: str,
        entities: dict,
    ) -> CodexReviewResult:
        return CodexReviewResult(
            brief_markdown="# Brief\n\n## Summary\n\nReviewed by Codex.",
            actions=[
                {
                    "title": "Validate the recommendation",
                    "why": "Primary source gap remains.",
                    "priority": "medium",
                }
            ],
            model_alias="codex",
        )


class _FakeMailer:
    def __init__(self, result: EmailDeliveryResult) -> None:
        self._result = result

    async def send(self, request: ResearchRequest, *, paths, body_markdown: str):  # noqa: ANN001
        return self._result


class _FakeMailxProcess:
    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        delay_seconds: float = 0.0,
    ) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self._delay_seconds = delay_seconds
        self.input_bytes = b""
        self.killed = False

    async def communicate(self, input: bytes) -> tuple[bytes, bytes]:
        self.input_bytes = input
        if self._delay_seconds:
            await asyncio.sleep(self._delay_seconds)
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


def _create_mail_paths(tmp_path: Path) -> ResearchJobPaths:
    paths = create_research_job_paths("Email test", base_dir=tmp_path)
    paths.report_path.write_text("# Report", encoding="utf-8")
    paths.brief_path.write_text("# Brief", encoding="utf-8")
    paths.facts_path.write_text("{}", encoding="utf-8")
    paths.actions_path.write_text("[]", encoding="utf-8")
    return paths


async def test_research_service_persists_artifacts(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ASH_HOME", str(tmp_path))

    backend = _FakeBackend(
        DeepAgentsRunResult(
            status="completed",
            backend="deepagents",
            report_text="# Report\n\nResult body.",
            sources_text="- https://example.com",
            transcript_text="# Transcript",
            notes={"note.md": "# Note\n\nDetail"},
            backend_run_dir=str(tmp_path / "deepagents-run"),
        )
    )
    service = ResearchService(
        config=None,
        backend=cast(Any, backend),
        extractor=cast(Any, _FakeExtractor()),
        reviewer=cast(Any, _FakeReviewer()),
        mailer=cast(
            Any,
            _FakeMailer(
                EmailDeliveryResult(
                    attempted=True,
                    sent=True,
                    recipient="rahul@example.com",
                    subject="[Ash Research] Compare agent stacks",
                )
            ),
        ),
    )

    result = await service.run(ResearchRequest(question="Compare agent stacks"))

    assert result.status == "completed"
    assert result.report_path.exists()
    assert result.brief_path.exists()
    assert result.facts_path.exists()
    assert result.actions_path.exists()
    assert result.sources_path.exists()
    assert (result.job_dir / "notes" / "note.md").exists()

    facts = json.loads(result.facts_path.read_text(encoding="utf-8"))
    assert facts["available"] is True
    assert facts["entities"]["organization"][0]["text"] == "OpenAI"

    actions = json.loads(result.actions_path.read_text(encoding="utf-8"))
    assert actions[0]["title"] == "Validate the recommendation"
    assert "Reviewed by Codex" in result.brief_path.read_text(encoding="utf-8")
    metadata = json.loads(
        (result.job_dir / "metadata.json").read_text(encoding="utf-8")
    )
    assert metadata["email"]["sent"] is True
    assert "rahul@example.com" in result.summary


async def test_research_service_handles_backend_unavailable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("ASH_HOME", str(tmp_path))

    backend = _FakeBackend(
        DeepAgentsRunResult(
            status="unavailable",
            backend="deepagents",
            error="missing deepagents repo",
        )
    )
    service = ResearchService(
        config=None,
        backend=cast(Any, backend),
        extractor=cast(Any, _FakeExtractor()),
        reviewer=None,
        mailer=cast(
            Any,
            _FakeMailer(
                EmailDeliveryResult(attempted=False, sent=False, error="no recipient")
            ),
        ),
    )

    result = await service.run(ResearchRequest(question="Track this topic"))

    assert result.status == "unavailable"
    report_text = result.report_path.read_text(encoding="utf-8")
    assert "missing deepagents repo" in report_text
    assert result.error == "missing deepagents repo"


def test_local_research_mailer_resolves_recipient_from_env(monkeypatch) -> None:
    monkeypatch.setenv("ASH_RESEARCH_EMAIL_TO", "rahul@example.com")
    mailer = LocalResearchMailer()

    recipient = mailer.resolve_recipient(ResearchRequest(question="Q"))

    assert recipient == "rahul@example.com"


def test_local_research_mailer_has_no_default_recipient(monkeypatch) -> None:
    monkeypatch.delenv("ASH_RESEARCH_EMAIL_TO", raising=False)
    mailer = LocalResearchMailer()

    recipient = mailer.resolve_recipient(ResearchRequest(question="Q"))

    assert recipient is None


async def test_local_research_mailer_skips_without_recipient(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("ASH_RESEARCH_EMAIL_TO", raising=False)
    mailer = LocalResearchMailer(env={})

    result = await mailer.send(
        ResearchRequest(question="Q", email_results=True),
        paths=_create_mail_paths(tmp_path),
        body_markdown="# Brief",
    )

    assert result.sent is False
    assert result.attempted is False
    assert result.error is not None
    assert "No email recipient configured" in result.error


async def test_local_research_mailer_sends_with_attachments(
    monkeypatch,
    tmp_path: Path,
) -> None:
    process = _FakeMailxProcess()
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    async def fake_create_subprocess_exec(
        *command: str,
        **kwargs: Any,
    ) -> _FakeMailxProcess:
        calls.append((command, kwargs))
        return process

    monkeypatch.setattr("ash.research.service.shutil.which", lambda _: "/usr/bin/mailx")
    monkeypatch.setattr(
        "ash.research.service.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    paths = _create_mail_paths(tmp_path)
    mailer = LocalResearchMailer(env={"ASH_RESEARCH_EMAIL_TO": "rahul@example.com"})

    result = await mailer.send(
        ResearchRequest(question="Compare agent stacks", email_results=True),
        paths=paths,
        body_markdown="# Brief",
    )

    command = calls[0][0]
    assert result.sent is True
    assert command[:3] == (
        "/usr/bin/mailx",
        "--subject",
        "[Ash Research] Compare agent stacks",
    )
    assert f"--attach={paths.report_path}" in command
    assert f"--attach={paths.brief_path}" in command
    assert f"--attach={paths.facts_path}" in command
    assert f"--attach={paths.actions_path}" in command
    assert command[-1] == "rahul@example.com"
    assert process.input_bytes == b"# Brief"


async def test_local_research_mailer_logs_delivery_lifecycle(
    caplog,
    monkeypatch,
    tmp_path: Path,
) -> None:
    async def fake_create_subprocess_exec(
        *command: str,
        **kwargs: Any,
    ) -> _FakeMailxProcess:
        return _FakeMailxProcess()

    monkeypatch.setattr("ash.research.service.shutil.which", lambda _: "/usr/bin/mailx")
    monkeypatch.setattr(
        "ash.research.service.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    mailer = LocalResearchMailer(env={"ASH_RESEARCH_EMAIL_TO": "rahul@example.com"})

    with caplog.at_level(logging.INFO, logger="ash.research.service"):
        result = await mailer.send(
            ResearchRequest(question="Compare agent stacks", email_results=True),
            paths=_create_mail_paths(tmp_path),
            body_markdown="# Brief",
        )

    assert result.sent is True
    assert "research_email_sending" in caplog.messages
    assert "research_email_sent" in caplog.messages


async def test_local_research_mailer_reports_mailx_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    async def fake_create_subprocess_exec(
        *command: str,
        **kwargs: Any,
    ) -> _FakeMailxProcess:
        return _FakeMailxProcess(returncode=1, stderr=b"delivery failed")

    monkeypatch.setattr("ash.research.service.shutil.which", lambda _: "/usr/bin/mailx")
    monkeypatch.setattr(
        "ash.research.service.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    mailer = LocalResearchMailer(env={"ASH_RESEARCH_EMAIL_TO": "rahul@example.com"})

    result = await mailer.send(
        ResearchRequest(question="Q", email_results=True),
        paths=_create_mail_paths(tmp_path),
        body_markdown="# Brief",
    )

    assert result.sent is False
    assert result.attempted is True
    assert result.error == "delivery failed"


async def test_local_research_mailer_times_out(monkeypatch, tmp_path: Path) -> None:
    process = _FakeMailxProcess(delay_seconds=1.0)

    async def fake_create_subprocess_exec(
        *command: str,
        **kwargs: Any,
    ) -> _FakeMailxProcess:
        return process

    monkeypatch.setattr("ash.research.service.shutil.which", lambda _: "/usr/bin/mailx")
    monkeypatch.setattr(
        "ash.research.service.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    mailer = LocalResearchMailer(
        env={"ASH_RESEARCH_EMAIL_TO": "rahul@example.com"},
        timeout_seconds=0.01,
    )

    result = await mailer.send(
        ResearchRequest(question="Q", email_results=True),
        paths=_create_mail_paths(tmp_path),
        body_markdown="# Brief",
    )

    assert result.sent is False
    assert process.killed is True
    assert result.error == "mailx timed out after 0.01s"


async def test_research_agent_passthrough_formats_result(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    service = MagicMock()
    service.run = AsyncMock(
        return_value=ResearchResult(
            status="completed",
            backend="deepagents",
            question="Question",
            job_dir=job_dir,
            brief_path=job_dir / "brief.md",
            report_path=job_dir / "report.md",
            facts_path=job_dir / "facts.json",
            sources_path=job_dir / "sources.md",
            actions_path=job_dir / "actions.json",
            summary="# Brief",
        )
    )
    agent = ResearchAgent(service)

    result = await agent.execute_passthrough(
        "Question",
        AgentContext(input_data={"mode": "demo"}),
    )

    assert result.is_error is False
    assert "Research job: completed" in result.content
    assert result.metadata["document_path"] == str(job_dir / "report.md")
    assert result.metadata["brief_path"] == str(job_dir / "brief.md")
    service.run.assert_awaited_once()


async def test_codex_reviewer_omits_temperature_for_llm_calls() -> None:
    llm = MagicMock()
    llm.complete = AsyncMock(
        side_effect=[
            CompletionResponse(
                message=Message(role=Role.ASSISTANT, content="# Brief"),
                stop_reason="end_turn",
                model="gpt-5.2",
            ),
            CompletionResponse(
                message=Message(role=Role.ASSISTANT, content="[]"),
                stop_reason="end_turn",
                model="gpt-5.2",
            ),
        ]
    )
    config = MagicMock()
    config.models = {"codex": object()}
    config.create_llm_provider_for_model.return_value = llm
    config.get_model.return_value = MagicMock(model="gpt-5.2")

    reviewer = CodexResearchReviewer(cast(Any, config))
    result = await reviewer.review(
        ResearchRequest(question="Question"),
        report_text="Report body",
        entities={"available": True},
    )

    assert result is not None
    for call in llm.complete.await_args_list:
        assert "temperature" not in call.kwargs


def test_research_service_injects_openai_key_from_config(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    secret = MagicMock()
    secret.get_secret_value.return_value = "sk-config-key"
    config = MagicMock()
    config.resolve_provider_api_key.return_value = secret

    service = ResearchService(config=cast(Any, config))

    assert service._backend._env["OPENAI_API_KEY"] == "sk-config-key"


def test_research_request_defaults_email_results_off() -> None:
    request = ResearchRequest.from_input("Question")

    assert request.email_results is False
