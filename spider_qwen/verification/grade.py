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

# Starting tier by source class (T-2.4 classify_source vocabulary).
_START_TIER: dict[str, int] = {
    "manufacturer": 4,
    "distributor": 4,
    "government": 4,
    "aggregator": 3,
    "business": 3,
    "broker": 2,
    "unknown": 2,
    "marketplace": 1,
}

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
    tier = _START_TIER.get(source_class, 2)
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
        start_tier=_TIER_VALUE[_START_TIER.get(source_class, 2)],
        reasons=reasons,
    )


def worst_grade(grades: list[str]) -> Grade:
    """Candidate-level grade: the weakest of its claims' grades."""
    order = {"high": 4, "moderate": 3, "low": 2, "very_low": 1}
    if not grades:
        return "very_low"
    return min(grades, key=lambda g: order.get(g, 1))  # type: ignore[return-value]
