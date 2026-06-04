"""GRADE-style evidence grading for procurement claims.

GRADE (Guyatt et al., J Clin Epi 2011) rates evidence by starting tier plus
named downgrade/upgrade factors. The procurement mapping:

- start tier   <- source type (bias): manufacturer/distributor/government start
                  High; aggregator/business Moderate; broker/unknown Low;
                  marketplace Very Low
- inconsistency <- contradicted/disputed values across sources   (-2)
- indirectness  <- claim grounded only at page level, no exact
                  character span                                  (-1)
                  or grounded only via a non-cited corpus span
                  (complementary / citation drift)                (-1)
- imprecision   <- wide Dempster-Shafer [Bel, Pl] interval        (-1)
- upgrade       <- three or more independent corroborating spans  (+1)

Output is a per-claim grade in {high, moderate, low, very_low} with the
reasons that produced it. Deterministic; every factor is a named rule.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Grade = Literal["high", "moderate", "low", "very_low"]

_TIER_VALUE: dict[int, Grade] = {4: "high", 3: "moderate", 2: "low", 1: "very_low"}
_GRADE_VALUE: dict[str, int] = {g: t for t, g in _TIER_VALUE.items()}


def _start_tier(source_class: str) -> int:
    """Starting tier derived from the T-2.4 reliability prior, so a source
    class added in governance/source_reliability.py gets a tier here without
    a second table to keep in sync (manufacturer/distributor/government 0.9+
    -> High; aggregator/business 0.6+ -> Moderate; broker/unknown 0.4+ -> Low;
    marketplace -> Very Low)."""
    # Lazy import: governance/__init__ -> modes.contracts -> evidence.models
    # would cycle at module load (same reason ledger.py imports lazily).
    from ..governance.source_reliability import DEFAULT_RELIABILITY

    r = DEFAULT_RELIABILITY.get(source_class, DEFAULT_RELIABILITY["unknown"])
    if r >= 0.9:
        return 4
    if r >= 0.6:
        return 3
    if r >= 0.4:
        return 2
    return 1

# DS interval width above which a claim is "imprecise".
WIDE_INTERVAL = 0.3
# Independent corroborating spans needed for the upgrade factor.
CORROBORATION_UPGRADE = 3


class GradeAssessment(BaseModel):
    grade: Grade
    start_tier: Grade
    reasons: list[str] = Field(default_factory=list)


def grade_claim(
    *,
    source_class: str,
    exact_span: bool = False,
    grounding: str = "grounded",
    ds_uncertainty: float | None = None,
    corroborating_spans: int = 1,
) -> GradeAssessment:
    """Grade one claim from its provenance and verification outcome."""
    tier = _start_tier(source_class)
    reasons = [f"start: {source_class} source"]

    if grounding == "contradicted":
        tier -= 2
        reasons.append("inconsistency: cited source contradicts the value (-2)")
    elif grounding == "ungrounded":
        tier -= 2
        reasons.append("inconsistency: value unsupported anywhere (-2)")
    elif grounding == "complementary":
        tier -= 1
        reasons.append("indirectness: grounded via a non-cited span (-1)")

    if not exact_span:
        tier -= 1
        reasons.append("indirectness: page-level grounding, no exact span (-1)")

    if ds_uncertainty is not None and ds_uncertainty > WIDE_INTERVAL:
        tier -= 1
        reasons.append(f"imprecision: [Bel, Pl] width {ds_uncertainty:.2f} (-1)")

    if grounding == "grounded" and corroborating_spans >= CORROBORATION_UPGRADE:
        tier += 1
        reasons.append(f"upgrade: {corroborating_spans} independent corroborating spans (+1)")

    tier = max(1, min(4, tier))
    return GradeAssessment(
        grade=_TIER_VALUE[tier],
        start_tier=_TIER_VALUE[_start_tier(source_class)],
        reasons=reasons,
    )


def worst_grade(grades: list[str]) -> Grade:
    """Candidate-level grade: the weakest of its claims' grades.

    Fail closed: an unrecognized grade string scores as very_low.
    """
    if not grades:
        return "very_low"
    known = [g if g in _GRADE_VALUE else "very_low" for g in grades]
    return min(known, key=_GRADE_VALUE.__getitem__)  # type: ignore[return-value]
