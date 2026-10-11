"""Publish existing Ash judgments without replacing the eval harness."""

from __future__ import annotations

import importlib
import os
from typing import TYPE_CHECKING, Any

from ash.observability.braintrust import scrub

if TYPE_CHECKING:
    from evals.runner import EvalResult


def result_record(suite: str, result: EvalResult) -> dict[str, Any]:
    scores: dict[str, float] = {
        "passed": float(result.passed and not result.is_judge_error)
    }
    if not result.is_judge_error:
        scores["quality"] = result.score
    return scrub(
        {
            "input": {"prompt": result.case.prompt},
            "output": {"response": result.response_text, "tools": result.tool_calls},
            "expected": result.case.expected_behavior,
            "scores": scores,
            "metadata": {
                "suite": suite,
                "case_id": result.case.id,
                "judge_error": result.is_judge_error,
                "error": result.error,
                "judge_reasoning": result.judge_result.reasoning,
                "criteria_scores": result.judge_result.criteria_scores,
            },
            "tags": ["ash", "eval", *result.case.tags],
        }
    )


class BraintrustExporter:
    def __init__(self, project: str, experiment: str | None = None) -> None:
        if not os.environ.get("BRAINTRUST_API_KEY"):
            raise ValueError("Set BRAINTRUST_API_KEY to export eval results")
        sdk = importlib.import_module("braintrust")
        self._experiment = sdk.init(
            project=project, experiment=experiment, set_current=False
        )

    def log(self, suite: str, result: EvalResult) -> None:
        self._experiment.log(**result_record(suite, result))

    def finish(self) -> str:
        self._experiment.flush()
        return str(self._experiment.summarize())
