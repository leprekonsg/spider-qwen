"""T-R.4: electronics_substitution output schema + deterministic grader.

Structured S1 output for the electronics vertical. The hard safety rule: a
substitute is only ``recommended`` (drop-in) when it has datasheet evidence,
high FFF similarity, and an active lifecycle -- otherwise it is at most
``needs_engineering_review``. A substitute with no evidence at all is ``rejected``
and excluded. Deterministic; the substitutes themselves are populated upstream by
the graph cross-references / Wayback / legacy-OCR miners.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .. import SCHEMA_VERSION
from ..evidence.models import EvidenceRef

Recommendation = Literal["recommended", "needs_engineering_review", "rejected"]
FFF_DROP_IN_THRESHOLD = 0.8


class InputPart(BaseModel):
    schema_version: str = SCHEMA_VERSION
    mpn: str
    manufacturer: str | None = None
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)


class SubstituteCandidate(BaseModel):
    schema_version: str = SCHEMA_VERSION
    mpn: str
    manufacturer: str | None = None
    fff_similarity: float = 0.0
    lifecycle_status: Literal["active", "nrnd", "eol", "unknown"] = "unknown"
    source_type: Literal["authorised_distributor", "broker", "archive", "unknown"] = "unknown"
    risk_score: float = 0.0
    has_datasheet_evidence: bool = False
    recommendation: Recommendation = "needs_engineering_review"
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)


class ElectronicsSubstitutionResult(BaseModel):
    schema_version: str = SCHEMA_VERSION
    mode: str = "electronics_substitution"
    input_part: InputPart
    substitute_candidates: list[SubstituteCandidate] = Field(default_factory=list)
    recommendation: Recommendation = "needs_engineering_review"
    assumptions_and_limits: list[str] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)


def grade_substitute(sub: SubstituteCandidate) -> Recommendation:
    if not sub.evidence_refs:
        return "rejected"
    # Hard rule: never claim drop-in without datasheet evidence.
    if sub.has_datasheet_evidence and sub.fff_similarity >= FFF_DROP_IN_THRESHOLD and sub.lifecycle_status == "active":
        return "recommended"
    return "needs_engineering_review"


def build_electronics_result(
    input_part: InputPart, candidates: list[SubstituteCandidate]
) -> ElectronicsSubstitutionResult:
    graded = [c.model_copy(update={"recommendation": grade_substitute(c)}) for c in candidates]
    kept = [c for c in graded if c.recommendation != "rejected"]
    kept.sort(key=lambda c: c.fff_similarity, reverse=True)

    if any(c.recommendation == "recommended" for c in kept):
        overall: Recommendation = "recommended"
    elif kept:
        overall = "needs_engineering_review"
    else:
        overall = "rejected"

    limits = [
        "Substitute candidates are for engineering review; FFF equivalence is not "
        "guaranteed without datasheet verification.",
    ]
    if any(not c.has_datasheet_evidence for c in kept):
        limits.append("Some candidates lack datasheet evidence and are not claimed drop-in.")
    if any(c.lifecycle_status == "unknown" for c in kept):
        limits.append("Lifecycle status is unconfirmed for some candidates.")

    seen: set[str] = set()
    refs: list[EvidenceRef] = []
    for ref in list(input_part.evidence_refs) + [r for c in kept for r in c.evidence_refs]:
        if ref.ledger_id not in seen:
            seen.add(ref.ledger_id)
            refs.append(ref)

    return ElectronicsSubstitutionResult(
        input_part=input_part, substitute_candidates=kept, recommendation=overall,
        assumptions_and_limits=limits, evidence_refs=refs,
    )
