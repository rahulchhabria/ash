"""Background evidence scoring and candidate dispatch. Spec: specs/quality.md."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import httpx

from ash.observability.braintrust import scrub
from ash.quality.models import DIMENSIONS, VERSION, Judgment
from ash.quality.scoring import evidence, judge_record, repairable, scores
from ash.quality.state import State


class BraintrustStore:
    def __init__(self, logger: Any, api_key: str):
        self.logger = logger
        self.project = str(uuid.UUID(logger.id))
        self.api_key = api_key

    async def fetch(self, limit: int) -> list[dict[str, Any]]:
        query = f"""select: id, input, output, metadata
        from: project_logs('{self.project}')
        filter: metadata.observation_kind = 'post_turn_session_snapshot' and
          (metadata.ash_quality.version is null or metadata.ash_quality.version != '{VERSION}')
        sort: created asc
        limit: {int(limit)}"""
        async with httpx.AsyncClient(timeout=30) as client:
            result = await client.post(
                "https://api.braintrust.dev/btql",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"query": query},
            )
            result.raise_for_status()
            return result.json()["data"]

    async def publish(self, job: dict[str, Any], judgment: Judgment | None) -> None:
        quality = {
            "version": VERSION,
            "status": job["status"],
            "case_id": job["id"],
            "repair_status": job["repair"],
            "judge_model": job.get("judge_model"),
        }
        if judgment:
            quality["assessments"] = judgment.model_dump()["assessments"]
        quality_scores: dict[str, float | None] = {
            f"ash_{dimension}": None for dimension in DIMENSIONS
        }
        if judgment:
            quality_scores.update(scores(judgment))
        await asyncio.to_thread(
            self.logger.update_span,
            id=job["row_id"],
            scores=quality_scores,
            metadata={"ash_quality": scrub(quality)},
        )
        await asyncio.to_thread(self.logger.flush)
        # SDK flush may exhaust retries without raising; verify the merge landed.
        row_id = str(uuid.UUID(job["row_id"]))
        async with httpx.AsyncClient(timeout=30) as client:
            for attempt in range(3):
                response = await client.post(
                    "https://api.braintrust.dev/btql",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "query": f"select: metadata, scores from: project_logs('{self.project}') filter: id = '{row_id}' limit: 1"
                    },
                )
                response.raise_for_status()
                rows = response.json()["data"]
                remote = (
                    (rows[0].get("metadata") or {}).get("ash_quality", {})
                    if rows
                    else {}
                )
                if (
                    remote.get("version") == VERSION
                    and remote.get("status") == job["status"]
                    and remote.get("repair_status") == job["repair"]
                    and all(
                        (rows[0].get("scores") or {}).get(k) == v
                        for k, v in quality_scores.items()
                    )
                ):
                    return
                await asyncio.sleep(attempt + 1)
        raise RuntimeError("Score publication not confirmed")


class Worker:
    def __init__(
        self,
        state: State,
        config: Any,
        store: BraintrustStore,
        client: Any,
        repo: Path,
        sentry: Any = None,
    ):
        self.state, self.config, self.store = state, config, store
        self.client, self.repo, self.sentry = client, repo, sentry

    async def process(self, key: str) -> None:
        job = self.state.get(key)
        judgment = (
            Judgment.model_validate_json(job["judgment"]) if job["judgment"] else None
        )
        if job["status"] == "pending":
            data = evidence(json.loads(job["record"]), self.config.max_input_chars)
            if data is None:
                self.state.update(key, status="insufficient_evidence")
            elif job["attempts"] >= self.config.max_attempts:
                self.state.update(key, status="judge_error")
            elif self.state.reserve("score", self.config.max_scores_per_day):
                self.state.update(key, attempts=job["attempts"] + 1)
                try:
                    judgment = await judge_record(
                        self.client, self.config.judge_model, data
                    )
                except Exception as exc:
                    self.state.update(
                        key,
                        error=type(exc).__name__,
                        status="judge_error"
                        if job["attempts"] + 1 >= self.config.max_attempts
                        else "pending",
                    )
                    return
                self.state.update(
                    key,
                    status="scored",
                    judgment=judgment.model_dump_json(),
                    repair="pending"
                    if repairable(judgment, self.config.failure_confidence)
                    else "ineligible",
                    error=None,
                )
            else:
                return
        job = self.state.get(key)
        if judgment and job["repair"] == "pending" and self.sentry:
            failed = [
                a.dimension
                for a in judgment.assessments
                if a.verdict == "fail"
                and a.confidence >= self.config.failure_confidence
            ]
            self.sentry.capture_event(
                {
                    "event_id": uuid.uuid5(uuid.NAMESPACE_URL, key).hex,
                    "level": "warning",
                    "message": "Ash conversation quality failure",
                    "fingerprint": ["ash-quality", VERSION, *sorted(failed)],
                    "tags": {"quality.version": VERSION, "quality.case_id": key},
                    "extra": {
                        "braintrust_project": self.store.project,
                        "braintrust_row": job["row_id"],
                        "dimensions": failed,
                    },
                }
            )
            await asyncio.to_thread(self.sentry.flush, timeout=10)
        job["judge_model"] = self.config.judge_model
        await self.store.publish(job, judgment)
        self.state.update(key, published=1)

    async def repair(self, key: str) -> None:
        from ash.quality.repair import build_candidate

        job = self.state.get(key)
        if job["repair"] != "pending" or job["status"] != "scored":
            raise ValueError("Case is not eligible for repair")
        if not self.config.repair_enabled:
            raise ValueError("Repairs are disabled")
        if not self.state.reserve("repair", self.config.max_repairs_per_day):
            return
        self.state.update(key, repair="running")
        try:
            result = await build_candidate(
                self.client,
                self.config.repair_model,
                self.repo,
                self.state.directory / "candidates" / key,
                json.loads(job["record"]),
                Judgment.model_validate_json(job["judgment"]),
            )
        except Exception as exc:
            result = "validation_failed"
            self.state.update(key, error=type(exc).__name__)
        self.state.update(key, repair=result, published=0)
        await self.process(key)

    async def once(self) -> None:
        records = await self.store.fetch(self.config.batch_size)
        for record in records:
            self.state.ingest(self.store.project, record)
        for job in self.state.pending(self.config.batch_size):
            await self.process(job["id"])
        if self.config.repair_enabled and (key := self.state.next_repair()):
            await self.repair(key)
