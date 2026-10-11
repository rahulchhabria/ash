from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from ash.quality.models import (
    DIMENSIONS,
    Assessment,
    Edit,
    Judgment,
    Proposal,
    QualityConfig,
)
from ash.quality.repair import apply_edits, one_test, read_tool
from ash.quality.scoring import evidence, scores, validate_judgment
from ash.quality.state import State
from ash.quality.worker import Worker


def record():
    return {
        "id": "00000000-0000-4000-8000-000000000001",
        "metadata": {
            "snapshot_version": 2,
            "turn_boundary_found": True,
            "content_captured": True,
            "provider": "test",
        },
        "input": {"request": "send it"},
        "output": {
            "turn_messages": [
                {"role": "user", "content": "send it"},
                {"role": "assistant", "content": "sent it"},
            ]
        },
    }


def judgment(verdict="fail"):
    return Judgment(
        assessments=[
            Assessment(
                dimension=d,
                verdict=verdict,
                confidence=0.99,
                reason="evidence",
                evidence=["sent it"] if verdict == "fail" else [],
            )
            for d in DIMENSIONS
        ]
    )


def test_evidence_rejects_synthetic_missing_truncated_and_old():
    assert evidence(record(), 5000)
    for overrides in (
        {"provider": "synthetic"},
        {"snapshot_version": 1},
        {"turn_truncated": True},
        {"content_captured": False},
        {"turn_boundary_found": False},
    ):
        row = record()
        row["metadata"].update(overrides)
        assert evidence(row, 5000) is None
    assert evidence(record(), 5) is None


def test_judge_quotes_and_unknown_are_not_success():
    data = evidence(record(), 5000)
    assert data is not None
    validate_judgment(judgment(), data)
    bad = judgment()
    bad.assessments[0].evidence = ["invented"]
    with pytest.raises(ValueError):
        validate_judgment(bad, data)
    assert scores(judgment("unknown")) == {}
    bad.assessments = bad.assessments[:1]
    with pytest.raises(ValueError):
        validate_judgment(bad, data)


def test_durable_budget_dedup_and_interruption(tmp_path):
    state = State(tmp_path)
    key = state.ingest("project", record())
    assert state.ingest("project", record()) == key
    assert state.reserve("repair", 1)
    state.update(key, repair="running")
    assert State.inspect(tmp_path)["recent_cases"][0]["repair"] == "running"
    assert state.get(key)["repair"] == "running"
    state.db.close()
    state = State(tmp_path)
    assert not state.reserve("repair", 1)
    assert state.get(key)["repair"] == "interrupted"
    state.db.close()


@pytest.mark.asyncio
async def test_publish_retry_never_rejudges(tmp_path):
    state = State(tmp_path)
    key = state.ingest("project", record())
    client = SimpleNamespace(
        responses=SimpleNamespace(
            parse=AsyncMock(return_value=SimpleNamespace(output_parsed=judgment()))
        )
    )
    store = SimpleNamespace(
        project="project", publish=AsyncMock(side_effect=[RuntimeError(), None])
    )
    worker = Worker(state, QualityConfig(), cast(Any, store), client, tmp_path)
    with pytest.raises(RuntimeError):
        await worker.process(key)
    assert state.get(key)["published"] == 0
    await worker.process(key)
    assert client.responses.parse.call_count == 1
    assert state.get(key)["published"] == 1
    assert state.get(key)["repair"] == "pending"


@pytest.mark.asyncio
async def test_judge_error_cannot_repair_and_budget_bounds_calls(tmp_path):
    state = State(tmp_path)
    key = state.ingest("project", record())
    client = SimpleNamespace(
        responses=SimpleNamespace(parse=AsyncMock(side_effect=RuntimeError()))
    )
    store = SimpleNamespace(publish=AsyncMock())
    worker = Worker(
        state,
        QualityConfig(max_attempts=1, max_scores_per_day=1),
        cast(Any, store),
        client,
        tmp_path,
    )
    await worker.process(key)
    await worker.process(key)
    assert state.get(key)["status"] == "judge_error"
    assert state.get(key)["repair"] == "ineligible"
    assert client.responses.parse.call_count == 1


def test_repair_path_and_exact_edit_guards(tmp_path):
    source = tmp_path / "src/ash/example.py"
    source.parent.mkdir(parents=True)
    source.write_text("answer = 1\n")
    for path in (
        "../vault",
        "src/ash/quality/worker.py",
        "tests/conftest.py",
        "/absolute/test",
    ):
        with pytest.raises(ValueError):
            apply_edits(
                tmp_path,
                {path},
                Proposal(
                    summary="fix",
                    regression_test="",
                    edits=[Edit(path=path, old="x", new="y")],
                ),
            )
    with pytest.raises(ValueError):
        read_tool(
            tmp_path,
            {"src/ash/example.py"},
            "read_file",
            json.dumps({"path": "../vault"}),
        )
    apply_edits(
        tmp_path,
        {"src/ash/example.py"},
        Proposal(
            summary="fix",
            regression_test="",
            edits=[Edit(path="src/ash/example.py", old="answer = 1", new="answer = 2")],
        ),
    )
    assert source.read_text() == "answer = 2\n"


def test_junit_rejects_errors_skips_empty_and_entities(tmp_path):
    report = tmp_path / "result.xml"
    for xml in (
        "<testsuite/>",
        "<testsuite><testcase><skipped/></testcase></testsuite>",
        "<testsuite><testcase><error/></testcase></testsuite>",
        "<!DOCTYPE x><testsuite/>",
    ):
        report.write_text(xml)
        assert not one_test(report, True)
    report.write_text(
        '<testsuite><testcase><failure type="AssertionError"/></testcase></testsuite>'
    )
    assert one_test(report, True)
    assert not one_test(report, False)


def test_real_isolated_candidate_requires_baseline_failure_and_green_suite(
    tmp_path,
):
    import asyncio
    import shutil
    import subprocess
    from pathlib import Path

    from ash.quality.repair import build_candidate

    if (
        shutil.which("bwrap") is None
        or not (Path(__file__).resolve().parents[1] / ".venv").is_dir()
    ):
        pytest.skip("Bubblewrap is required for repair sandbox tests")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitignore").write_text(".venv\n")
    (repo / ".venv").symlink_to(
        Path(__file__).resolve().parents[1] / ".venv", target_is_directory=True
    )
    (repo / "src/ash").mkdir(parents=True)
    (repo / "src/ash/__init__.py").write_text("")
    (repo / "src/ash/example.py").write_text("def answer():\n    return 1\n")
    (repo / "tests").mkdir()
    (repo / "tests/test_existing.py").write_text(
        "from ash.example import answer\ndef test_existing():\n    assert isinstance(answer(), int)\ndef test_other():\n    assert callable(answer)\n"
    )
    for args in (
        ["init", "-q"],
        ["add", "."],
        [
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        ],
    ):
        subprocess.run(  # noqa: S603 - fixed fixture commands
            ["/usr/bin/git", *args], cwd=repo, check=True, capture_output=True
        )
    revision = subprocess.check_output(
        ["/usr/bin/git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    test = """import os
import socket
from pathlib import Path

from ash.example import answer


def test_regression():
    assert not Path("/home/rahul/.ash/config.toml").exists()
    assert "OPENAI_API_KEY" not in os.environ
    try:
        Path("src/ash/example.py").write_text("changed")
    except OSError:
        pass
    else:
        raise AssertionError("source writable")
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=1)
    except OSError:
        pass
    else:
        raise AssertionError("network accessible")
    assert answer() == 2
"""
    proposal = Proposal(
        summary="Fixture repair",
        regression_test=test,
        edits=[Edit(path="src/ash/example.py", old="return 1", new="return 2")],
    )
    client = SimpleNamespace(
        responses=SimpleNamespace(
            parse=AsyncMock(return_value=SimpleNamespace(output_parsed=proposal))
        )
    )
    row = record()
    row["metadata"]["release"] = revision
    artifact = tmp_path / "candidate"
    status = asyncio.run(
        build_candidate(client, "test", repo, artifact, row, judgment())
    )
    assert status == "review_ready", (artifact / "report.json").read_text()
    assert "return 1" in (repo / "src/ash/example.py").read_text()
    assert (artifact / "candidate.patch").is_file()
    assert (artifact / "source/.quality-reports/suite.xml").is_file()
