from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

VERSION = "ash-quality-v1"
DIMENSIONS = ("grounding", "instruction_following", "honest_completion")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Assessment(StrictModel):
    dimension: Literal["grounding", "instruction_following", "honest_completion"]
    verdict: Literal["pass", "fail", "unknown"]
    confidence: float = Field(ge=0, le=1)
    reason: str
    evidence: list[str]


class Judgment(StrictModel):
    assessments: list[Assessment]


class Edit(StrictModel):
    path: str
    old: str
    new: str


class Proposal(StrictModel):
    summary: str
    regression_test: str
    edits: list[Edit]


class QualityConfig(BaseModel):
    enabled: bool = False
    auto_push: bool = False
    push_remote: str = "origin"
    push_url: str = ""
    sentry_org: str = ""
    sentry_project: str = ""
    review_model: str = "gpt-5.2"
    judge_model: str = "gpt-5.2"
    repair_model: str = "gpt-5.2"
    poll_seconds: int = Field(default=120, ge=30)
    batch_size: int = Field(default=10, ge=1, le=50)
    max_scores_per_day: int = Field(default=100, ge=0, le=1000)
    max_repairs_per_day: int = Field(default=1, ge=0, le=10)
    repair_enabled: bool = False
    failure_confidence: float = Field(default=0.9, ge=0.8, le=1)
    max_attempts: int = Field(default=3, ge=1, le=5)
    max_input_chars: int = Field(default=50000, ge=1000, le=100000)


class Diagnosis(StrictModel):
    category: Literal[
        "code_defect", "configuration", "external", "insufficient_evidence"
    ]
    confidence: float = Field(ge=0, le=1)
    reason: str
    evidence: list[str]


class Review(StrictModel):
    approved: bool
    addresses_evidence: bool
    regression_is_meaningful: bool
    preserves_safety: bool
    reason: str
