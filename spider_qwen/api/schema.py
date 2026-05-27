"""Top-level run result schema returned by the controller and CLI."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .. import SCHEMA_VERSION
from ..evidence.models import EvidenceRef


class Classification(BaseModel):
    mode: str
    confidence: float
    rationale: str = ""


class RunResult(BaseModel):
    schema_version: str = SCHEMA_VERSION
    run_id: str
    query: str
    mode: str
    stop_reason: str
    classification: Classification
    validated_candidates: list[dict[str, Any]] = Field(default_factory=list)
    pricing_status_summary: dict[str, int] = Field(default_factory=dict)
    rfq_drafts: list[dict[str, Any]] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)
