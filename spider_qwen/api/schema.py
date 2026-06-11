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
    # T-1.1: four-slot serendipity view (primary_answer + s1/s2/s3 + score).
    serendipity: dict[str, Any] | None = None
    # T-8.2: opt-in (--serendipity) discovery sidecar -- S1/S2/S3 populated from
    # real components (graph/Wayback/signals/DMSMS). None unless requested.
    serendipity_discovery: dict[str, Any] | None = None
    pricing_status_summary: dict[str, int] = Field(default_factory=dict)
    rfq_drafts: list[dict[str, Any]] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    # RFC 6962 inclusion proofs for the final citations, bound to the run's
    # persisted tree-head commitment (+ signed_tree_head when STH signing is
    # configured). Empty when the run does not persist (no commitment exists).
    citation_proofs: list[dict[str, Any]] = Field(default_factory=list)
    # One composed trust verdict per validated candidate: verified-claim
    # counts, GRADE, GSAR decision, [Bel, Pl] interval, conformal status,
    # disputed facts, and a prose summary -- the single place the end-to-end
    # trust story for a vendor is stated.
    trust_verdicts: list[dict[str, Any]] = Field(default_factory=list)
    # Which Qwen seams existed this run (router, JSON extractor, NLI, query
    # rewriter, RFQ drafter, page judge), whether each was live or mocked, and
    # what it was invoked for. Honest by construction: an offline judged demo
    # reports mock=true rather than implying live model calls.
    qwen_paths: dict[str, Any] = Field(default_factory=dict)
    # Discovery reasoning trace: initial queries, the CRAG retrieval verdict,
    # corrective/replan pivot queries, and which rewriter proposed them.
    reasoning: dict[str, Any] | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)
