"""DeepAgents-backed research jobs with optional GLiNER and Codex post-processing."""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from ash.config.paths import get_ash_home

if TYPE_CHECKING:
    from ash.config import AshConfig
    from ash.llm import LLMProvider

logger = logging.getLogger(__name__)

DEFAULT_GLINER_MODEL = "fastino/gliner2-base-v1"
DEFAULT_EMAIL_SUBJECT_PREFIX = "[Ash Research]"
DEFAULT_GLINER_LABELS: dict[str, str] = {
    "person": "Named person or author",
    "organization": "Company, institution, standards body, or vendor",
    "product": "Product, platform, service, or tool name",
    "package": "Library, package, framework, or SDK name",
    "api": "API, endpoint family, protocol, or interface name",
    "version": "Version string, release number, or model identifier",
    "date": "Date, launch date, deadline, or milestone",
    "metric": "Quoted metric, benchmark, price, or numeric claim",
    "location": "City, region, or country",
}
_RUN_DIR_RE = re.compile(r"^Run directory:\s*(.+)$", re.MULTILINE)
_REPORT_RE = re.compile(r"^Report:\s*(.+)$", re.MULTILINE)
_SOURCES_RE = re.compile(r"^Sources:\s*(.+)$", re.MULTILINE)
_SLUG_RE = re.compile(r"[^a-z0-9]+")
ResearchMode = Literal["smoke", "demo", "full"]
ResearchStatus = Literal["completed", "partial", "unavailable", "failed"]


def _normalize_research_mode(value: object) -> ResearchMode:
    raw = str(value).strip().lower()
    if raw in {"smoke", "demo", "full"}:
        return cast(ResearchMode, raw)
    return "demo"


@dataclass(frozen=True)
class ResearchRequest:
    """User-facing research request."""

    question: str
    mode: ResearchMode = "demo"
    labels: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_GLINER_LABELS))
    deepagents_path: Path | None = None
    deepagents_model: str | None = None
    max_search_results: int | None = None
    codex_model_alias: str | None = "codex"
    codex_review: bool = True
    email_results: bool = False
    email_to: str | None = None
    timeout_seconds: int = 900

    @classmethod
    def from_input(
        cls,
        question: str,
        input_data: dict[str, Any] | None = None,
    ) -> ResearchRequest:
        """Build a request from `use_agent` input data."""
        data = input_data or {}
        mode = _normalize_research_mode(data.get("mode", "demo"))

        raw_labels = data.get("labels")
        labels = (
            {
                str(key): str(value)
                for key, value in raw_labels.items()
                if str(key).strip() and str(value).strip()
            }
            if isinstance(raw_labels, dict)
            else dict(DEFAULT_GLINER_LABELS)
        )

        deepagents_path = data.get("deepagents_path")
        timeout_seconds = data.get("timeout_seconds", 900)

        return cls(
            question=question.strip(),
            mode=mode,
            labels=labels,
            deepagents_path=(
                Path(str(deepagents_path)).expanduser().resolve()
                if deepagents_path
                else None
            ),
            deepagents_model=_optional_text(data.get("deepagents_model")),
            max_search_results=_optional_int(data.get("max_search_results")),
            codex_model_alias=_optional_text(data.get("codex_model_alias")) or "codex",
            codex_review=bool(data.get("codex_review", True)),
            email_results=bool(data.get("email_results", False)),
            email_to=_optional_text(data.get("email_to")),
            timeout_seconds=max(30, _optional_int(timeout_seconds) or 900),
        )


@dataclass(frozen=True)
class ResearchJobPaths:
    """Filesystem layout for one Ash research job."""

    job_id: str
    job_dir: Path
    request_path: Path
    report_path: Path
    brief_path: Path
    sources_path: Path
    facts_path: Path
    actions_path: Path
    transcript_path: Path
    logs_path: Path
    metadata_path: Path
    notes_dir: Path
    raw_dir: Path


def create_research_job_paths(
    question: str,
    *,
    base_dir: Path | None = None,
) -> ResearchJobPaths:
    """Create a timestamped research job directory."""
    root = (base_dir or get_ash_home() / "research" / "jobs").resolve()
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    slug = _slugify(question)[:48] or "research"
    job_id = f"{timestamp}-{slug}"
    job_dir = root / job_id
    notes_dir = job_dir / "notes"
    raw_dir = job_dir / "raw"
    notes_dir.mkdir(parents=True, exist_ok=False)
    raw_dir.mkdir(parents=True, exist_ok=True)
    return ResearchJobPaths(
        job_id=job_id,
        job_dir=job_dir,
        request_path=job_dir / "request.md",
        report_path=job_dir / "report.md",
        brief_path=job_dir / "brief.md",
        sources_path=job_dir / "sources.md",
        facts_path=job_dir / "facts.json",
        actions_path=job_dir / "actions.json",
        transcript_path=job_dir / "transcript.md",
        logs_path=job_dir / "backend.log",
        metadata_path=job_dir / "metadata.json",
        notes_dir=notes_dir,
        raw_dir=raw_dir,
    )


@dataclass(frozen=True)
class DeepAgentsRunResult:
    """Normalized result from the DeepAgents backend."""

    status: Literal["completed", "unavailable", "failed"]
    backend: str
    report_text: str = ""
    sources_text: str = ""
    transcript_text: str = ""
    notes: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    backend_run_dir: str | None = None
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class CodexReviewResult:
    """Optional Codex review outputs."""

    brief_markdown: str
    actions: list[dict[str, str]]
    model_alias: str


@dataclass(frozen=True)
class EmailDeliveryResult:
    """Result of local email delivery."""

    attempted: bool
    sent: bool
    recipient: str | None = None
    subject: str | None = None
    error: str | None = None


def _existing_email_attachments(paths: ResearchJobPaths) -> list[str]:
    attachments = [
        paths.report_path,
        paths.brief_path,
        paths.facts_path,
        paths.actions_path,
    ]
    return [str(path) for path in attachments if path.exists()]


@dataclass(frozen=True)
class ResearchResult:
    """Final research job result."""

    status: ResearchStatus
    backend: str
    question: str
    job_dir: Path
    brief_path: Path
    report_path: Path
    facts_path: Path
    sources_path: Path
    actions_path: Path
    summary: str
    error: str | None = None

    def to_user_message(self) -> str:
        """Render a concise user-facing completion message."""
        lines = [
            f"Research job: {self.status}",
            f"Backend: {self.backend}",
            f"Job directory: {self.job_dir}",
            f"Brief: {self.brief_path}",
            f"Report: {self.report_path}",
            f"Facts: {self.facts_path}",
        ]
        if self.error:
            lines.append(f"Error: {self.error}")
        if self.summary:
            lines.extend(["", self.summary.strip()])
        return "\n".join(lines)


class DeepAgentsResearchBackend:
    """Run the local DeepAgents research workflow when available."""

    def __init__(
        self,
        *,
        default_project_root: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._default_project_root = (
            default_project_root.expanduser().resolve()
            if default_project_root
            else None
        )
        self._env = dict(os.environ if env is None else env)

    def resolve_project_root(self, request: ResearchRequest) -> Path | None:
        """Resolve the DeepAgents project root from request, env, or local defaults."""
        candidates: list[Path] = []
        if request.deepagents_path is not None:
            candidates.append(request.deepagents_path)
        if self._default_project_root is not None:
            candidates.append(self._default_project_root)
        if env_path := self._env.get("ASH_DEEPAGENTS_PATH"):
            candidates.append(Path(env_path).expanduser().resolve())
        candidates.append(Path.home() / "GitHub" / "deepagents")

        for candidate in candidates:
            if (candidate / "research").exists() and (candidate / "agent.py").exists():
                return candidate
        return None

    async def run(
        self,
        request: ResearchRequest,
        paths: ResearchJobPaths,
    ) -> DeepAgentsRunResult:
        """Execute the local DeepAgents workflow and normalize the output."""
        project_root = self.resolve_project_root(request)
        if project_root is None:
            return DeepAgentsRunResult(
                status="unavailable",
                backend="deepagents",
                error=(
                    "DeepAgents runner not found. Set ASH_DEEPAGENTS_PATH or place the "
                    "repo at ~/GitHub/deepagents."
                ),
            )

        script_path = project_root / "research"
        runs_dir = project_root / "runs"
        before_runs = _list_run_dirs(runs_dir)
        command = [str(script_path), "--mode", request.mode, "--no-stream"]
        if request.deepagents_model:
            command.extend(["--model", request.deepagents_model])
            command.extend(["--researcher-model", request.deepagents_model])

        max_search_results = request.max_search_results
        if max_search_results is None:
            if request.mode == "full":
                max_search_results = 10
            elif request.mode == "demo":
                max_search_results = 6

        if max_search_results is not None:
            command.extend(["--max-search-results", str(max(1, max_search_results))])
        command.append(request.question)

        env = dict(self._env)
        env["PYTHONUNBUFFERED"] = "1"

        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(project_root),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=request.timeout_seconds,
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            return DeepAgentsRunResult(
                status="failed",
                backend="deepagents",
                error=f"DeepAgents timed out after {request.timeout_seconds} seconds.",
            )

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        paths.logs_path.write_text(
            "\n".join(
                [
                    "# DeepAgents Backend Log",
                    "",
                    "## Command",
                    "",
                    "```text",
                    " ".join(command),
                    "```",
                    "",
                    "## Stdout",
                    "",
                    "```text",
                    stdout,
                    "```",
                    "",
                    "## Stderr",
                    "",
                    "```text",
                    stderr,
                    "```",
                ]
            ),
            encoding="utf-8",
        )

        if process.returncode != 0:
            return DeepAgentsRunResult(
                status="failed",
                backend="deepagents",
                error=stderr.strip() or stdout.strip() or "DeepAgents run failed.",
                stdout=stdout,
                stderr=stderr,
            )

        backend_run_dir = _extract_backend_run_dir(stdout, runs_dir, before_runs)
        if backend_run_dir is None:
            return DeepAgentsRunResult(
                status="failed",
                backend="deepagents",
                error="DeepAgents completed but no run directory could be resolved.",
                stdout=stdout,
                stderr=stderr,
            )

        report_path = _extract_report_path(stdout, backend_run_dir)
        sources_path = _extract_sources_path(stdout, backend_run_dir)
        transcript_path = backend_run_dir / "transcript.md"
        notes_dir = backend_run_dir / "notes"

        report_text = _read_text_if_exists(report_path)
        sources_text = _read_text_if_exists(sources_path)
        transcript_text = _read_text_if_exists(transcript_path)
        notes = {
            note_path.name: note_path.read_text(encoding="utf-8")
            for note_path in sorted(notes_dir.glob("*.md"))
            if note_path.is_file()
        }

        return DeepAgentsRunResult(
            status="completed",
            backend="deepagents",
            report_text=report_text,
            sources_text=sources_text,
            transcript_text=transcript_text,
            notes=notes,
            backend_run_dir=str(backend_run_dir),
            stdout=stdout,
            stderr=stderr,
        )


class GLiNERResearchExtractor:
    """Optional GLiNER2 entity extraction over research artifacts."""

    def __init__(self, model_name: str = DEFAULT_GLINER_MODEL) -> None:
        self._model_name = model_name
        self._model: Any | None = None

    async def extract(
        self,
        text: str,
        labels: dict[str, str],
    ) -> dict[str, Any]:
        """Extract entities from the combined report text."""
        if not text.strip():
            return {"available": False, "reason": "empty_text", "entities": {}}
        try:
            return await asyncio.to_thread(self._extract_sync, text, labels)
        except ModuleNotFoundError:
            return {
                "available": False,
                "reason": "gliner2_not_installed",
                "entities": {},
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("research_gliner_failed", exc_info=True)
            return {
                "available": False,
                "reason": f"gliner_failed: {exc}",
                "entities": {},
            }

    def _extract_sync(self, text: str, labels: dict[str, str]) -> dict[str, Any]:
        gliner2 = importlib.import_module("gliner2")
        model_cls = gliner2.GLiNER2

        if self._model is None:
            self._model = model_cls.from_pretrained(self._model_name)

        # Keep inference bounded for practical latency.
        clipped = text[:24_000]
        result = self._model.extract_entities(
            clipped,
            labels,
            threshold=0.35,
            format_results=True,
            include_confidence=True,
        )
        return {
            "available": True,
            "model": self._model_name,
            "labels": labels,
            "entities": result.get("entities", {}),
        }


class CodexResearchReviewer:
    """Optional final review/synthesis stage using Ash's LLM config."""

    def __init__(self, config: AshConfig) -> None:
        self._config = config

    async def review(
        self,
        request: ResearchRequest,
        report_text: str,
        entities: dict[str, Any],
    ) -> CodexReviewResult | None:
        """Create a concise brief and next actions."""
        alias = self._resolve_model_alias(request.codex_model_alias)
        if alias is None:
            return None

        try:
            llm = self._config.create_llm_provider_for_model(alias)
            model = self._config.get_model(alias).model
        except Exception:
            logger.warning("research_codex_disabled", exc_info=True)
            return None

        brief_markdown = await self._generate_brief(
            llm=llm,
            model=model,
            question=request.question,
            report_text=report_text,
            entities=entities,
        )
        actions = await self._generate_actions(
            llm=llm,
            model=model,
            question=request.question,
            report_text=report_text,
        )
        return CodexReviewResult(
            brief_markdown=brief_markdown,
            actions=actions,
            model_alias=alias,
        )

    def _resolve_model_alias(self, preferred: str | None) -> str | None:
        if preferred and preferred in self._config.models:
            return preferred
        if "codex" in self._config.models:
            return "codex"
        if "default" in self._config.models:
            return "default"
        return None

    async def _generate_brief(
        self,
        *,
        llm: LLMProvider,
        model: str,
        question: str,
        report_text: str,
        entities: dict[str, Any],
    ) -> str:
        from ash.llm.types import Message, Role

        prompt = "\n".join(
            [
                f"Research question: {question}",
                "",
                "Write a concise markdown brief with these sections:",
                "## Summary",
                "## Key Findings",
                "## Risks And Gaps",
                "## Next Actions",
                "",
                "Keep it practical and grounded in the supplied material.",
                "",
                "Entities JSON:",
                json.dumps(entities, indent=2, sort_keys=True)[:6000],
                "",
                "Report excerpt:",
                report_text[:12000],
            ]
        )
        response = await llm.complete(
            messages=[Message(role=Role.USER, content=prompt)],
            model=model,
            system=(
                "You are Codex acting as the final research reviewer. "
                "Turn raw research into a compact, high-signal brief. "
                "Do not invent sources or facts."
            ),
            max_tokens=1600,
        )
        return response.message.get_text() or ""

    async def _generate_actions(
        self,
        *,
        llm: LLMProvider,
        model: str,
        question: str,
        report_text: str,
    ) -> list[dict[str, str]]:
        from ash.llm.types import Message, Role

        prompt = "\n".join(
            [
                f"Research question: {question}",
                "",
                "Return JSON only.",
                "Produce an array of up to 5 action objects with keys:",
                '- "title"',
                '- "why"',
                '- "priority"  # low | medium | high',
                "",
                "Use only actions justified by the report. If no action is warranted, return [].",
                "",
                "Report excerpt:",
                report_text[:10000],
            ]
        )
        response = await llm.complete(
            messages=[Message(role=Role.USER, content=prompt)],
            model=model,
            system=(
                "You are Codex extracting actionable next steps from research. "
                "Return strict JSON and nothing else."
            ),
            max_tokens=700,
        )
        raw = (response.message.get_text() or "").strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        actions: list[dict[str, str]] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title", "")).strip()
            why = str(item.get("why", "")).strip()
            priority = str(item.get("priority", "")).strip().lower()
            if not title or not why or priority not in {"low", "medium", "high"}:
                continue
            actions.append(
                {
                    "title": title,
                    "why": why,
                    "priority": priority,
                }
            )
        return actions


class LocalResearchMailer:
    """Send research outputs through the local mailx/sendmail stack."""

    def __init__(
        self,
        *,
        env: dict[str, str] | None = None,
        command: str = "mailx",
        timeout_seconds: float = 30.0,
    ) -> None:
        self._env = dict(os.environ if env is None else env)
        self._command = command
        self._timeout_seconds = timeout_seconds

    def resolve_recipient(self, request: ResearchRequest) -> str | None:
        """Resolve email recipient from request or environment."""
        return request.email_to or _optional_text(
            self._env.get("ASH_RESEARCH_EMAIL_TO")
        )

    def build_subject(self, request: ResearchRequest) -> str:
        """Build a stable email subject."""
        prefix = (
            _optional_text(self._env.get("ASH_RESEARCH_EMAIL_SUBJECT_PREFIX"))
            or DEFAULT_EMAIL_SUBJECT_PREFIX
        )
        return f"{prefix} {request.question[:120].strip()}"

    async def send(
        self,
        request: ResearchRequest,
        *,
        paths: ResearchJobPaths,
        body_markdown: str,
    ) -> EmailDeliveryResult:
        """Send the research result with attachments if configured."""
        if not request.email_results:
            logger.info("research_email_skipped", extra={"email.reason": "disabled"})
            return EmailDeliveryResult(attempted=False, sent=False)

        recipient = self.resolve_recipient(request)
        if not recipient:
            logger.warning(
                "research_email_skipped",
                extra={"email.reason": "recipient_missing"},
            )
            return EmailDeliveryResult(
                attempted=False,
                sent=False,
                error=(
                    "No email recipient configured. Set ASH_RESEARCH_EMAIL_TO or pass "
                    "email_to in the request."
                ),
            )

        mailx_path = shutil.which(self._command)
        if not mailx_path:
            logger.warning(
                "research_email_failed",
                extra={
                    "email.reason": "command_missing",
                    "process.command": self._command,
                },
            )
            return EmailDeliveryResult(
                attempted=True,
                sent=False,
                recipient=recipient,
                error=f"{self._command} is not installed.",
            )

        subject = self.build_subject(request)
        attachments = await asyncio.to_thread(_existing_email_attachments, paths)
        command = [mailx_path, "--subject", subject]
        for attachment in attachments:
            command.append(f"--attach={attachment}")
        command.append(recipient)
        logger.info(
            "research_email_sending",
            extra={
                "email.attachment_count": len(attachments),
                "process.command": self._command,
            },
        )

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env,
            )
        except OSError as exc:
            logger.warning(
                "research_email_failed",
                extra={
                    "email.reason": "process_start_failed",
                    "error.type": type(exc).__name__,
                    "error.message": str(exc),
                    "process.command": self._command,
                },
            )
            return EmailDeliveryResult(
                attempted=True,
                sent=False,
                recipient=recipient,
                subject=subject,
                error=f"failed to start {self._command}: {exc}",
            )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(body_markdown.encode("utf-8")),
                timeout=self._timeout_seconds,
            )
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
            logger.warning(
                "research_email_failed",
                extra={
                    "email.reason": "timeout",
                    "email.timeout_seconds": self._timeout_seconds,
                    "process.command": self._command,
                },
            )
            return EmailDeliveryResult(
                attempted=True,
                sent=False,
                recipient=recipient,
                subject=subject,
                error=f"{self._command} timed out after {self._timeout_seconds:g}s",
            )
        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()

        if process.returncode != 0:
            logger.warning(
                "research_email_failed",
                extra={
                    "email.reason": "nonzero_exit",
                    "process.command": self._command,
                    "process.exit_code": process.returncode,
                },
            )
            return EmailDeliveryResult(
                attempted=True,
                sent=False,
                recipient=recipient,
                subject=subject,
                error=stderr or stdout or "mailx failed",
            )

        logger.info(
            "research_email_sent",
            extra={
                "email.attachment_count": len(attachments),
                "process.command": self._command,
            },
        )
        return EmailDeliveryResult(
            attempted=True,
            sent=True,
            recipient=recipient,
            subject=subject,
        )


class ResearchService:
    """Run research end-to-end and persist normalized artifacts."""

    def __init__(
        self,
        *,
        config: AshConfig | None = None,
        backend: DeepAgentsResearchBackend | None = None,
        extractor: GLiNERResearchExtractor | None = None,
        reviewer: CodexResearchReviewer | None = None,
        mailer: LocalResearchMailer | None = None,
    ) -> None:
        self._config = config
        self._backend = backend or DeepAgentsResearchBackend(
            env=self._build_backend_env(config)
        )
        self._extractor = extractor or GLiNERResearchExtractor()
        self._reviewer = reviewer or (
            CodexResearchReviewer(config) if config is not None else None
        )
        self._mailer = mailer or LocalResearchMailer()

    @staticmethod
    def _build_backend_env(config: AshConfig | None) -> dict[str, str]:
        """Build DeepAgents subprocess env from host env plus Ash config secrets."""
        env = dict(os.environ)
        if config is None:
            return env

        try:
            api_key = config.resolve_provider_api_key("openai")
        except Exception:
            api_key = None
        if api_key is not None:
            secret = api_key.get_secret_value().strip()
            if secret:
                env["OPENAI_API_KEY"] = secret
        try:
            research_agent = config.agents.get("research")
            preferred_alias = (
                research_agent.model
                if research_agent is not None and research_agent.model
                else "default"
            )
            preferred_model = config.get_model(preferred_alias).model
        except Exception:
            preferred_model = None
        if preferred_model:
            # Keep the DeepAgents coordinator and researcher aligned with Ash's
            # configured default unless the caller explicitly overrides them.
            env.setdefault("OPENAI_MODEL", preferred_model)
            env.setdefault("OPENAI_RESEARCHER_MODEL", preferred_model)
        return env

    async def run(self, request: ResearchRequest) -> ResearchResult:
        """Run a research request through backend, extraction, and review."""
        if not request.question.strip():
            raise ValueError("question is required")

        paths = create_research_job_paths(request.question)
        self._write_request_file(paths, request)

        backend_result = await self._backend.run(request, paths)
        persisted_status = backend_result.status
        error = backend_result.error

        report_text = backend_result.report_text
        if backend_result.status != "completed":
            report_text = self._build_backend_fallback_report(request, backend_result)
        self._write_text(paths.report_path, report_text)
        self._write_text(paths.sources_path, backend_result.sources_text)
        self._write_text(paths.transcript_path, backend_result.transcript_text)
        self._persist_notes(paths, backend_result.notes)
        self._persist_raw_backend(paths, backend_result)

        extraction_input = self._build_extraction_input(report_text, backend_result)
        entities = await self._extractor.extract(extraction_input, request.labels)
        self._write_json(paths.facts_path, entities)

        review_result: CodexReviewResult | None = None
        if request.codex_review and self._reviewer is not None and report_text.strip():
            review_result = await self._reviewer.review(request, report_text, entities)

        brief_markdown = (
            review_result.brief_markdown
            if review_result is not None and review_result.brief_markdown.strip()
            else self._fallback_brief(request, report_text, backend_result)
        )
        actions = review_result.actions if review_result is not None else []
        self._write_text(paths.brief_path, brief_markdown)
        self._write_json(paths.actions_path, actions)

        email_result = await self._mailer.send(
            request,
            paths=paths,
            body_markdown=brief_markdown,
        )

        status: ResearchStatus
        if persisted_status == "completed":
            status = "completed"
        elif persisted_status == "unavailable":
            status = "unavailable"
        elif report_text.strip():
            status = "partial"
        else:
            status = "failed"

        metadata = {
            "job_id": paths.job_id,
            "status": status,
            "backend": backend_result.backend,
            "backend_status": backend_result.status,
            "backend_run_dir": backend_result.backend_run_dir,
            "error": error,
            "question": request.question,
            "mode": request.mode,
            "gliner_available": bool(entities.get("available")),
            "codex_model_alias": (
                review_result.model_alias if review_result is not None else None
            ),
            "email": {
                "attempted": email_result.attempted,
                "sent": email_result.sent,
                "recipient": email_result.recipient,
                "subject": email_result.subject,
                "error": email_result.error,
            },
            "created_at": datetime.now(UTC).isoformat(),
        }
        self._write_json(paths.metadata_path, metadata)

        summary = brief_markdown
        if email_result.sent and email_result.recipient:
            summary = f"{brief_markdown}\n\nEmail sent to `{email_result.recipient}`."
        elif email_result.error:
            summary = f"{brief_markdown}\n\nEmail delivery note: {email_result.error}"

        return ResearchResult(
            status=status,
            backend=backend_result.backend,
            question=request.question,
            job_dir=paths.job_dir,
            brief_path=paths.brief_path,
            report_path=paths.report_path,
            facts_path=paths.facts_path,
            sources_path=paths.sources_path,
            actions_path=paths.actions_path,
            summary=summary,
            error=error,
        )

    def _write_request_file(
        self,
        paths: ResearchJobPaths,
        request: ResearchRequest,
    ) -> None:
        self._write_text(
            paths.request_path,
            "\n".join(
                [
                    "# Research Request",
                    "",
                    f"- Job ID: `{paths.job_id}`",
                    f"- Created: `{datetime.now(UTC).isoformat()}`",
                    f"- Mode: `{request.mode}`",
                    f"- DeepAgents model: `{request.deepagents_model or 'default'}`",
                    f"- Codex review: `{request.codex_review}`",
                    "",
                    "## Question",
                    "",
                    request.question,
                    "",
                    "## GLiNER Labels",
                    "",
                    "```json",
                    json.dumps(request.labels, indent=2, sort_keys=True),
                    "```",
                ]
            ),
        )

    def _persist_notes(self, paths: ResearchJobPaths, notes: dict[str, str]) -> None:
        for name, content in sorted(notes.items()):
            safe_name = name if name.endswith(".md") else f"{name}.md"
            self._write_text(paths.notes_dir / safe_name, content)

    def _persist_raw_backend(
        self,
        paths: ResearchJobPaths,
        result: DeepAgentsRunResult,
    ) -> None:
        raw_report = paths.raw_dir / "backend_report.md"
        raw_sources = paths.raw_dir / "backend_sources.md"
        raw_stdout = paths.raw_dir / "stdout.txt"
        raw_stderr = paths.raw_dir / "stderr.txt"
        self._write_text(raw_report, result.report_text)
        self._write_text(raw_sources, result.sources_text)
        self._write_text(raw_stdout, result.stdout)
        self._write_text(raw_stderr, result.stderr)

    def _build_backend_fallback_report(
        self,
        request: ResearchRequest,
        result: DeepAgentsRunResult,
    ) -> str:
        lines = [
            "# Research Report",
            "",
            f"Research backend status: `{result.status}`",
            "",
            f"Question: {request.question}",
        ]
        if result.error:
            lines.extend(["", "## Backend Error", "", result.error])
        lines.extend(
            [
                "",
                "## Notes",
                "",
                "No external DeepAgents report was produced for this run.",
            ]
        )
        return "\n".join(lines)

    def _build_extraction_input(
        self,
        report_text: str,
        result: DeepAgentsRunResult,
    ) -> str:
        parts = [report_text]
        if result.sources_text:
            parts.extend(["", "## Sources", "", result.sources_text[:5000]])
        for name, content in sorted(result.notes.items()):
            parts.extend(["", f"## Note: {name}", "", content[:4000]])
        return "\n".join(parts)

    def _fallback_brief(
        self,
        request: ResearchRequest,
        report_text: str,
        result: DeepAgentsRunResult,
    ) -> str:
        excerpt = report_text.strip()[:1500]
        lines = [
            "# Brief",
            "",
            f"- Question: {request.question}",
            f"- Backend: {result.backend}",
            f"- Backend status: {result.status}",
        ]
        if result.error:
            lines.append(f"- Error: {result.error}")
        if excerpt:
            lines.extend(["", "## Report Excerpt", "", excerpt])
        return "\n".join(lines)

    @staticmethod
    def _write_text(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )


def _extract_backend_run_dir(
    stdout: str,
    runs_dir: Path,
    before_runs: set[Path],
) -> Path | None:
    match = _RUN_DIR_RE.search(stdout)
    if match:
        path = Path(match.group(1).strip()).expanduser()
        if path.exists():
            return path.resolve()

    after_runs = _list_run_dirs(runs_dir)
    new_runs = sorted(after_runs - before_runs)
    if new_runs:
        return new_runs[-1]
    if after_runs:
        return sorted(after_runs)[-1]
    return None


def _extract_report_path(stdout: str, run_dir: Path) -> Path:
    match = _REPORT_RE.search(stdout)
    if match:
        return Path(match.group(1).strip()).expanduser().resolve()
    return run_dir / "report.md"


def _extract_sources_path(stdout: str, run_dir: Path) -> Path:
    match = _SOURCES_RE.search(stdout)
    if match:
        return Path(match.group(1).strip()).expanduser().resolve()
    return run_dir / "sources.md"


def _list_run_dirs(runs_dir: Path) -> set[Path]:
    if not runs_dir.exists():
        return set()
    return {path.resolve() for path in runs_dir.iterdir() if path.is_dir()}


def _read_text_if_exists(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _slugify(value: str) -> str:
    lowered = value.lower().strip()
    compact = _SLUG_RE.sub("-", lowered)
    return compact.strip("-")


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
