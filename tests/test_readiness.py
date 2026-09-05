import asyncio

import pytest

from spider_qwen.api.factory import build_controller
from spider_qwen.evidence.models import EvidenceRef
from spider_qwen.modes.contracts import ServiceCandidate
from spider_qwen.verification.readiness import candidate_readiness


def _candidate(**changes):
    return ServiceCandidate(
        vendor_name="Acme Cleaning", website="https://acme.com.sg",
        evidence_completeness=1, checklist_completeness=1,
        evidence_refs=[EvidenceRef(ledger_id="ev_a", url="https://acme.com.sg",
                                  snippet_hash="hash", retrieved_at="2026-09-05T00:00:00Z")],
    ).model_copy(update=changes)


@pytest.mark.parametrize("changes,assessment_changes,offline,stage,reason", [
    ({}, {}, False, "review_ready", None),
    ({}, {}, True, "evidence_checked", "offline_fixture"),
    ({"checklist_completeness": 0.5}, {}, False, "evidence_checked", "incomplete_rfq_inputs"),
    ({"evidence_completeness": 0.5}, {}, False, "evidence_checked", "incomplete_evidence"),
    ({}, {"grade": "low"}, False, "evidence_checked", "evidence_below_review_floor"),
    ({}, {"grade": None}, False, "evidence_checked", "evidence_below_review_floor"),
    ({}, {"claims_unsupported": 1}, False, "discovered", "unsupported_or_unassessed_claims"),
    ({}, {"claims_verified": 0}, False, "discovered", "no_verified_claims"),
    ({"conflicting_fields": ["country"]}, {}, False, "discovered", "unresolved_conflicts"),
])
def test_readiness_requires_evidence_not_score(changes, assessment_changes, offline, stage, reason):
    assessment = {"claims_verified": 2, "claims_unsupported": 0, "decision": "proceed", "grade": "moderate"}
    assessment.update(assessment_changes)
    result = candidate_readiness(_candidate(score=100, **changes), assessment,
                                 verification_enabled=True, offline=offline, grade_floor="moderate")
    assert result["stage"] == stage
    assert result["approval"] == "not_recorded"
    if reason:
        assert reason in result["reasons"]


def test_real_offline_output_cannot_claim_review_ready(tmp_path, no_network):
    result = asyncio.run(build_controller(offline=True, state_dir=str(tmp_path)).run("office cleaning Singapore"))
    assert result.validated_candidates
    for candidate in result.validated_candidates:
        readiness = candidate["readiness"]
        assert readiness["stage"] == "discovered"
        assert "claim_verification_disabled" in readiness["reasons"]
        verdict = next(v for v in result.trust_verdicts if v["supplier_id"] == candidate["supplier_id"])
        assert verdict["readiness"] == readiness
