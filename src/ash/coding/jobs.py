"""Persistent coding job state for Telegram-first coding workflows."""

from __future__ import annotations

import json
import logging
import uuid
from builtins import list as builtin_list
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ash.config.paths import get_ash_home

logger = logging.getLogger(__name__)

JobStatus = Literal[
    "planned",
    "running",
    "waiting_approval",
    "testing",
    "ready",
    "merged",
    "cancelled",
    "failed",
]


@dataclass
class CodingJob:
    """A durable coding task controlled from a chat provider."""

    id: str
    task: str
    repo_path: str
    chat_id: str | None = None
    user_id: str | None = None
    provider: str | None = None
    thread_id: str | None = None
    branch: str | None = None
    project_name: str | None = None
    changed_files: list[str] = field(default_factory=list)
    last_pr_url: str | None = None
    last_checkpoint: str | None = None
    status: JobStatus = "planned"
    telegram_message_id: str | None = None
    last_diff_summary: str | None = None
    last_test_command: str | None = None
    last_test_result: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    @classmethod
    def create(
        cls,
        *,
        task: str,
        repo_path: str,
        chat_id: str | None = None,
        user_id: str | None = None,
        provider: str | None = None,
        thread_id: str | None = None,
        telegram_message_id: str | None = None,
    ) -> CodingJob:
        return cls(
            id=f"code-{uuid.uuid4().hex[:10]}",
            task=task,
            repo_path=repo_path,
            chat_id=chat_id,
            user_id=user_id,
            provider=provider,
            thread_id=thread_id,
            telegram_message_id=telegram_message_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CodingJob:
        if "changed_files" not in data or data["changed_files"] is None:
            data["changed_files"] = []
        return cls(**data)

    def touch(self) -> None:
        self.updated_at = datetime.now(UTC).isoformat()


class CodingJobStore:
    """JSON-file backed store for coding jobs."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or get_ash_home() / "coding" / "jobs"
        self.root.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        *,
        task: str,
        repo_path: str,
        chat_id: str | None = None,
        user_id: str | None = None,
        provider: str | None = None,
        thread_id: str | None = None,
        telegram_message_id: str | None = None,
    ) -> CodingJob:
        job = CodingJob.create(
            task=task,
            repo_path=repo_path,
            chat_id=chat_id,
            user_id=user_id,
            provider=provider,
            thread_id=thread_id,
            telegram_message_id=telegram_message_id,
        )
        self.save(job)
        return job

    def save(self, job: CodingJob) -> None:
        job.touch()
        self._path(job.id).write_text(
            json.dumps(job.to_dict(), indent=2, sort_keys=True) + "\n"
        )

    def get(self, job_id: str) -> CodingJob | None:
        path = self._path(job_id)
        if not path.exists():
            return None
        return CodingJob.from_dict(json.loads(path.read_text()))

    def list(self, *, limit: int = 10) -> builtin_list[CodingJob]:
        jobs: list[CodingJob] = []
        for path in sorted(self.root.glob("code-*.json"), reverse=True):
            try:
                jobs.append(CodingJob.from_dict(json.loads(path.read_text())))
            except Exception as exc:
                logger.debug(
                    "coding_job_load_failed",
                    extra={"file.path": str(path), "error.message": str(exc)},
                )
                continue
            if len(jobs) >= limit:
                break
        return jobs

    def latest_for_chat(
        self,
        *,
        chat_id: str | None,
        user_id: str | None,
        provider: str | None,
    ) -> CodingJob | None:
        for job in self.list(limit=50):
            if provider and job.provider != provider:
                continue
            if chat_id and job.chat_id != chat_id:
                continue
            if user_id and job.user_id != user_id:
                continue
            if job.status not in {"cancelled", "merged", "failed"}:
                return job
        return None

    def _path(self, job_id: str) -> Path:
        safe = "".join(ch for ch in job_id if ch.isalnum() or ch in {"-", "_"})
        return self.root / f"{safe}.json"
