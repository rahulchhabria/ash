"""Promotion uses a real local Git remote; no provider calls or production pushes."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest

from ash.quality.models import Diagnosis, QualityConfig
from ash.quality.promotion import git, promote, write_json
from ash.quality.state import State
from ash.quality.worker import Worker

CASE = "a" * 24
TEST_PATH = "tests/test_quality_regression_0123456789abcdef.py"


@pytest.fixture
def promotion_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    remote = tmp_path / "remote.git"
    git(tmp_path, ["init", "--bare", str(remote)])
    git(repo, ["init", "-b", "main"])
    git(repo, ["config", "user.name", "Test"])
    git(repo, ["config", "user.email", "test@example.invalid"])
    (repo / "src/ash").mkdir(parents=True)
    (repo / "src/ash/example.py").write_text("answer = 1\n")
    git(repo, ["add", "."])
    git(repo, ["commit", "-m", "baseline"])
    git(repo, ["remote", "add", "origin", str(remote)])
    git(repo, ["push", "origin", "main"])
    base = git(repo, ["rev-parse", "HEAD"])
    artifact = tmp_path / "candidate"
    values = {
        "src/ash/example.py": "answer = 2\n",
        TEST_PATH: "def test_answer():\n    assert True\n",
    }
    for name, value in values.items():
        path = artifact / "source" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
    (artifact / "candidate.patch").write_text("fixture patch")
    report = {
        "base_revision": base,
        "status": "review_ready",
        "files": {n: hashlib.sha256(v.encode()).hexdigest() for n, v in values.items()},
        "patch_sha256": hashlib.sha256(b"fixture patch").hexdigest(),
    }
    write_json(artifact / "report.json", report)
    write_json(artifact / "review.json", {**report, "approved": True})
    return repo, artifact, str(remote), base


def test_promotion_and_unknown_push_outcome_are_idempotent(promotion_repo, monkeypatch):
    import ash.quality.promotion as promotion

    repo, artifact, remote, base = promotion_repo
    real_git = promotion.git
    fail_once = True

    def lose_push_response(repo, args, **kwargs):
        nonlocal fail_once
        result = real_git(repo, args, **kwargs)
        if args[0] == "push" and fail_once:
            fail_once = False
            raise TimeoutError("response lost after successful push")
        return result

    monkeypatch.setattr(promotion, "git", lose_push_response)
    with pytest.raises(TimeoutError):
        promote(repo, artifact, "origin", remote, CASE)
    prepared = json.loads((artifact / "promotion.json").read_text())["commit"]
    assert promote(repo, artifact, "origin", remote, CASE) == "pushed"
    assert promote(repo, artifact, "origin", remote, CASE) == "pushed"
    assert git(repo, ["rev-parse", "HEAD"]) == base
    assert git(repo, ["status", "--porcelain"]) == ""
    assert git(repo, ["show", prepared + ":src/ash/example.py"]) == "answer = 2"
    assert git(repo, ["rev-list", "--count", base + ".." + prepared]) == "1"


def test_promotion_refuses_stale_base_changed_remote_and_tampering(promotion_repo):
    repo, artifact, remote, _ = promotion_repo
    with pytest.raises(ValueError, match="Remote URL changed"):
        promote(repo, artifact, "origin", "https://example.invalid/repo", CASE)
    source = artifact / "source/src/ash/example.py"
    source.write_text("tampered\n")
    with pytest.raises(ValueError, match="Validated source changed"):
        promote(repo, artifact, "origin", remote, CASE)
    source.write_text("answer = 2\n")
    git(repo, ["commit", "--allow-empty", "-m", "concurrent main change"])
    git(repo, ["push", "origin", "main"])
    assert promote(repo, artifact, "origin", remote, CASE) == "stale_base"
    assert not (artifact / "promotion.json").exists()


@pytest.mark.asyncio
async def test_sentry_collection_is_redacted_paginated_and_excludes_feedback(
    monkeypatch,
):
    from ash.quality.sentry import SentrySource

    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/issues/"):
            return httpx.Response(
                200,
                json=[
                    {"id": "1", "shortId": "PIGEON-1", "title": "user me@example.com"},
                    {"id": "2"},
                ],
                headers={
                    "Link": '<https://sentry.io/api/0/projects/o/p/issues/?cursor=next-page>; rel="next"; results="true"'
                },
            )
        if "/issues/2/" in request.url.path:
            return httpx.Response(
                200, json={"tags": [{"key": "quality.version", "value": "v1"}]}
            )
        return httpx.Response(
            200,
            json={
                "release": {"version": "trace-verification-working-tree"},
                "entries": [],
                "eventID": "event",
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: original(**kw, transport=httpx.MockTransport(handler)),
    )
    records, cursor = await SentrySource("secret", "o", "p").fetch(5, "previous")
    assert cursor == "next-page"
    assert len(records) == 1
    assert records[0]["metadata"]["provider"] == "synthetic"
    assert "me@example.com" not in json.dumps(records)
    assert requests[0].url.params["cursor"] == "previous"


@pytest.mark.asyncio
async def test_source_outage_does_not_block_other_source_or_spend_repair_budget(
    tmp_path,
):
    state = State(tmp_path)
    incident = {
        "id": "1:release",
        "metadata": {"source": "sentry", "provider": "production"},
        "title": "timeout",
    }
    source = SimpleNamespace(
        project="sentry:o/p", fetch=AsyncMock(return_value=([incident], None))
    )
    store = SimpleNamespace(
        fetch=AsyncMock(side_effect=RuntimeError()), publish=AsyncMock()
    )
    diagnosis = Diagnosis(
        category="external",
        confidence=0.99,
        reason="Provider timeout",
        evidence=["timeout"],
    )
    client = SimpleNamespace(
        responses=SimpleNamespace(
            parse=AsyncMock(return_value=SimpleNamespace(output_parsed=diagnosis))
        )
    )
    worker = Worker(
        state,
        QualityConfig(repair_enabled=True),
        cast(Any, store),
        client,
        tmp_path,
        sentry_source=source,
    )
    await worker.once()
    assert state.status()["recent_cases"][0]["repair"] == "ineligible"
    assert state.cursor("braintrust_error") == "RuntimeError"
    store.publish.assert_not_called()
    await worker.once()
    assert client.responses.parse.call_count == 1
    assert state.status()["budgets"] == [
        {"day": state.status()["budgets"][0]["day"], "kind": "score", "used": 1}
    ]


@pytest.mark.asyncio
async def test_rejected_review_never_pushes(promotion_repo, tmp_path, monkeypatch):
    import ash.quality.promotion as promotion
    from ash.quality.models import Review

    repo, artifact, remote, base = promotion_repo
    state = State(tmp_path / "state")
    key = state.ingest("project", {"id": "row"})
    destination = state.directory / "candidates" / key
    destination.parent.mkdir()
    artifact.rename(destination)
    state.update(key, status="scored", repair="review_ready", published=1)
    client = SimpleNamespace(
        responses=SimpleNamespace(
            parse=AsyncMock(
                return_value=SimpleNamespace(
                    output_parsed=Review(
                        approved=False,
                        addresses_evidence=False,
                        regression_is_meaningful=False,
                        preserves_safety=True,
                        reason="Tautological test",
                    )
                )
            )
        )
    )
    push = AsyncMock()
    monkeypatch.setattr(promotion, "promote", push)
    worker = Worker(
        state,
        QualityConfig(auto_push=True, push_url=remote),
        cast(Any, SimpleNamespace()),
        client,
        repo,
    )
    await worker.promote(key)
    assert state.get(key)["repair"] == "review_rejected"
    push.assert_not_called()
    assert git(repo, ["rev-parse", "HEAD"]) == base


def test_concurrent_push_cannot_overwrite_main(promotion_repo, monkeypatch):
    import ash.quality.promotion as promotion

    repo, artifact, remote, base = promotion_repo
    original = promotion.git
    other = None

    def racing_git(path, args, **kwargs):
        nonlocal other
        if args[0] == "push" and other is None:
            tree = original(repo, ["rev-parse", base + "^{tree}"])
            other = original(
                repo, ["commit-tree", tree, "-p", base], data="Other writer\n"
            )
            original(repo, ["push", "origin", other + ":refs/heads/main"])
        return original(path, args, **kwargs)

    monkeypatch.setattr(promotion, "git", racing_git)
    with pytest.raises(RuntimeError, match="Git operation failed: push"):
        promote(repo, artifact, "origin", remote, CASE)
    assert promote(repo, artifact, "origin", remote, CASE) == "stale_base"
    assert git(repo, ["ls-remote", "origin", "refs/heads/main"]).split()[0] == other


def test_read_only_status_supports_pre_migration_database(tmp_path):
    state = State(tmp_path)
    state.db.execute("DROP TABLE cursors")
    state.db.commit()
    assert State.inspect(tmp_path)["sources"] == []
    assert (
        state.db.execute("SELECT 1 FROM sqlite_master WHERE name='cursors'").fetchone()
        is None
    )
    state.db.close()


@pytest.mark.asyncio
async def test_incident_quotes_use_original_values_not_json_or_labels():
    from ash.quality.sentry import diagnose

    title = 'Request failed: "bad value"\nretry later'
    diagnosis = Diagnosis(
        category="external", confidence=0.99, reason="Timeout", evidence=[title]
    )
    parse = AsyncMock(return_value=SimpleNamespace(output_parsed=diagnosis))
    client = SimpleNamespace(responses=SimpleNamespace(parse=parse))
    assert await diagnose(client, "test", {"title": title}) == diagnosis
    for quote in ('title: "Request failed"', '"title":', "invented", "  "):
        diagnosis.evidence = [quote]
        with pytest.raises(ValueError, match="requires source evidence"):
            await diagnose(client, "test", {"title": title})
