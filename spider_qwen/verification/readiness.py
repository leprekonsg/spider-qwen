"""Explain research readiness without implying human approval."""

from .grade import grade_at_least


def candidate_readiness(candidate, assessment: dict, *, verification_enabled: bool,
                        offline: bool, grade_floor: str, disputed: bool = False) -> dict:
    reasons = []
    if not verification_enabled:
        reasons.append("claim_verification_disabled")
    elif not assessment or not assessment.get("claims_verified"):
        reasons.append("no_verified_claims")
    if assessment.get("claims_unsupported") != 0:
        reasons.append("unsupported_or_unassessed_claims")
    if assessment.get("decision") != "proceed":
        reasons.append("verification_did_not_proceed")
    if disputed or getattr(candidate, "conflicting_fields", []):
        reasons.append("unresolved_conflicts")
    if reasons:
        return {"stage": "discovered", "reasons": reasons, "approval": "not_recorded"}

    if offline:
        reasons.append("offline_fixture")
    if getattr(candidate, "evidence_completeness", 0) < 1:
        reasons.append("incomplete_evidence")
    if hasattr(candidate, "checklist_completeness") and candidate.checklist_completeness < 1:
        reasons.append("incomplete_rfq_inputs")
    grade = assessment.get("grade")
    if grade not in {"very_low", "low", "moderate", "high"} or not grade_at_least(grade, grade_floor):
        reasons.append("evidence_below_review_floor")
    if not getattr(candidate, "evidence_refs", []):
        reasons.append("missing_evidence_references")
    return {"stage": "evidence_checked" if reasons else "review_ready",
            "reasons": reasons, "approval": "not_recorded"}
