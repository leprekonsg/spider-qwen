"""T-R.4: electronics_substitution vertical mode.

A first-class mode on the reasoning spine with a structured output schema. Hard
rule: never claim a drop-in (recommended) substitute without datasheet evidence.
Additive -- the deviation #11 mapping (product/revalidation) stays as fallback;
existing classifications are unchanged.
"""

from __future__ import annotations

from spider_qwen.evidence.models import EvidenceRef, utc_now_iso
from spider_qwen.modes.classifier import ModeClassifier
from spider_qwen.modes.contracts import ProcurementMode
from spider_qwen.modes.electronics import (
    ElectronicsSubstitutionResult,
    InputPart,
    SubstituteCandidate,
    build_electronics_result,
)
from spider_qwen.modes.router import ModeRouter
from spider_qwen.reasoning.process_reward import weights_for, ELECTRONICS_WEIGHTS
from spider_qwen.reasoning.trajectory import ELECTRONICS_STRATEGIES, strategies_for


def _ref(lid: str) -> EvidenceRef:
    return EvidenceRef(ledger_id=lid, url=f"https://{lid}.example", snippet_hash="h", retrieved_at=utc_now_iso())


# --- classification (additive, non-regressive) ------------------------------

def test_obsolete_replacement_query_classifies_as_electronics_substitution():
    res = ModeClassifier().classify("find a replacement for an obsolete Hirose DF13-6P-1.25DSA, deliver to Singapore")
    assert res.mode == ProcurementMode.ELECTRONICS_SUBSTITUTION


def test_mpn_substitute_query_classifies_as_electronics_substitution():
    assert ModeClassifier().classify("NE5532 substitute cross reference").mode == ProcurementMode.ELECTRONICS_SUBSTITUTION


def test_existing_classifications_unchanged():
    assert ModeClassifier().classify("office cleaning Singapore").mode == ProcurementMode.SERVICE_QUOTE_REQUIRED
    assert ModeClassifier().classify(
        "500 ergonomic office chairs Singapore with public pricing"
    ).mode == ProcurementMode.PRODUCT_EXACT_PRICE
    assert ModeClassifier().classify(
        "find contact email for Example Cleaning Pte Ltd"
    ).mode == ProcurementMode.CONTACT_ENRICHMENT_ONLY


def test_router_and_spine_support_the_mode():
    route = ModeRouter().route(ProcurementMode.ELECTRONICS_SUBSTITUTION)
    assert route.ranker == "product"
    assert route.produces_rfq is False
    assert route.serendipity_mode is True
    # The reasoning spine routes this mode through electronics strategies + weights.
    assert strategies_for("electronics_substitution") == ELECTRONICS_STRATEGIES
    assert weights_for("electronics_substitution") is ELECTRONICS_WEIGHTS


# --- structured output + drop-in safety rule --------------------------------

def test_drop_in_recommended_requires_datasheet_evidence():
    part = InputPart(mpn="DF13-6P-1.25DSA", manufacturer="Hirose", evidence_refs=[_ref("ev_part")])
    with_ds = SubstituteCandidate(mpn="DF13C-6P-1.25DSA", manufacturer="Hirose", fff_similarity=0.95,
                                  lifecycle_status="active", source_type="authorised_distributor",
                                  has_datasheet_evidence=True, evidence_refs=[_ref("ev_ds")])
    without_ds = SubstituteCandidate(mpn="GENERIC-6P", fff_similarity=0.95, lifecycle_status="active",
                                     source_type="broker", has_datasheet_evidence=False, evidence_refs=[_ref("ev_b")])
    result = build_electronics_result(part, [with_ds, without_ds])
    assert isinstance(result, ElectronicsSubstitutionResult)
    by_mpn = {c.mpn: c for c in result.substitute_candidates}
    assert by_mpn["DF13C-6P-1.25DSA"].recommendation == "recommended"
    assert by_mpn["GENERIC-6P"].recommendation == "needs_engineering_review"  # no datasheet -> never drop-in


def test_substitute_without_evidence_is_rejected_and_excluded():
    part = InputPart(mpn="NE5532")
    no_ev = SubstituteCandidate(mpn="PHANTOM-OP", fff_similarity=0.99, lifecycle_status="active", evidence_refs=[])
    result = build_electronics_result(part, [no_ev])
    assert all(c.mpn != "PHANTOM-OP" for c in result.substitute_candidates)  # rejected -> excluded
    assert result.recommendation == "rejected"


def test_result_carries_assumptions_and_limits():
    part = InputPart(mpn="NE5532", evidence_refs=[_ref("ev_p")])
    sub = SubstituteCandidate(mpn="NE5532A", fff_similarity=0.7, lifecycle_status="unknown",
                              has_datasheet_evidence=False, evidence_refs=[_ref("ev_s")])
    result = build_electronics_result(part, [sub])
    assert result.recommendation == "needs_engineering_review"
    assert result.assumptions_and_limits
    assert any("engineering review" in a.lower() for a in result.assumptions_and_limits)
