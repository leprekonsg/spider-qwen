"""GSAR typed grounding + GRADE claim grading."""

from __future__ import annotations

from spider_qwen.verification.grade import grade_claim, worst_grade
from spider_qwen.verification.grounding import (
    classify_grounding,
    detect_numeric_contradiction,
    worst_decision,
)


# --- typed grounding ---------------------------------------------------------

def test_grounded_proceeds():
    r = classify_grounding(supported_on_cited=True, supported_on_corpus=False)
    assert r.label == "grounded" and r.decision == "proceed"


def test_complementary_regenerates():
    r = classify_grounding(supported_on_cited=False, supported_on_corpus=True)
    assert r.label == "complementary" and r.decision == "regenerate"


def test_contradicted_replans():
    span = "Acme Supplies quotes SGD 90 per unit for this service."
    r = classify_grounding(supported_on_cited=False, supported_on_corpus=False,
                           subject="Acme Supplies", value="120", cited_span=span)
    assert r.label == "contradicted" and r.decision == "replan"


def test_ungrounded_regenerates():
    r = classify_grounding(supported_on_cited=False, supported_on_corpus=False,
                           subject="Acme", value="120",
                           cited_span="Acme provides cleaning services in Singapore.")
    assert r.label == "ungrounded" and r.decision == "regenerate"


def test_numeric_contradiction_requires_subject_colocation():
    # A different vendor's price must not contradict Acme's claim.
    span = "Bizco quotes SGD 90. Contact us for more."
    assert not detect_numeric_contradiction("Acme Supplies", "120", span)
    # Acme co-located with a different number does contradict.
    assert detect_numeric_contradiction("Acme Supplies", "120",
                                        "Acme Supplies quotes SGD 90 per visit.")
    # Acme co-located with the claimed number does not.
    assert not detect_numeric_contradiction("Acme Supplies", "120",
                                            "Acme Supplies quotes SGD 120 per visit.")


def test_non_numeric_values_never_auto_contradict():
    span = "Acme Supplies: reach us at other@acme.sg."
    assert not detect_numeric_contradiction("Acme Supplies", "sales@acme.sg", span)


def test_worst_decision_aggregation():
    assert worst_decision([]) == "proceed"
    assert worst_decision(["proceed", "proceed"]) == "proceed"
    assert worst_decision(["proceed", "regenerate"]) == "regenerate"
    assert worst_decision(["regenerate", "replan", "proceed"]) == "replan"


# --- GRADE -------------------------------------------------------------------

def test_manufacturer_with_exact_span_grades_high():
    g = grade_claim(source_class="manufacturer", exact_span=True, grounding="grounded")
    assert g.grade == "high" and g.start_tier == "high"


def test_page_level_grounding_downgrades_once():
    g = grade_claim(source_class="distributor", exact_span=False, grounding="grounded")
    assert g.grade == "moderate"
    assert any("page-level" in r for r in g.reasons)


def test_contradiction_downgrades_twice():
    g = grade_claim(source_class="manufacturer", exact_span=True, grounding="contradicted")
    assert g.grade == "low"


def test_marketplace_floor_is_very_low():
    g = grade_claim(source_class="marketplace", exact_span=False, grounding="ungrounded")
    assert g.grade == "very_low"


def test_wide_ds_interval_downgrades():
    g = grade_claim(source_class="distributor", exact_span=True,
                    grounding="grounded", ds_uncertainty=0.5)
    assert g.grade == "moderate"
    assert any("imprecision" in r for r in g.reasons)


def test_corroboration_upgrades_grounded_claims_only():
    g = grade_claim(source_class="aggregator", exact_span=True,
                    grounding="grounded", corroborating_spans=3)
    assert g.grade == "high"
    g2 = grade_claim(source_class="aggregator", exact_span=True,
                     grounding="complementary", corroborating_spans=5)
    assert g2.grade == "low"  # -1 indirectness, no upgrade for non-grounded


def test_worst_grade_aggregation():
    assert worst_grade([]) == "very_low"
    assert worst_grade(["high", "moderate", "low"]) == "low"
    assert worst_grade(["high", "high"]) == "high"


# --- spine integration ---------------------------------------------------------

def _spine_candidate(page_text: str, price: float):
    from types import SimpleNamespace

    from spider_qwen.evidence.ledger import EvidenceLedger
    from spider_qwen.modes.contracts import PricingStatus

    ledger = EvidenceLedger("run_gsar", None)
    page_ref = ledger.record(source_tool="tinyfish_fetch", url="https://acme.sg/chairs",
                             snippet=page_text[:100], text=page_text, metadata={})
    value = str(int(price))
    start = page_text.find(value)
    metadata = {"field": "pricing", "claim_id": "claim_g1",
                "parent_ledger_id": page_ref.ledger_id}
    if start >= 0:
        metadata.update({"start_char": start, "end_char": start + len(value)})
    claim_ref = ledger.record(source_tool="tinyfish_fetch", url="https://acme.sg/chairs",
                              snippet=value, text=None, metadata=metadata)
    cand = SimpleNamespace(vendor_name="Acme", price=price, currency="SGD", unit="unit",
                           moq=None, pricing_status=PricingStatus.EXACT_PRICE,
                           evidence_refs=[page_ref, claim_ref])
    return ledger, cand, claim_ref


def test_spine_emits_grounded_proceed_with_grade():
    from spider_qwen.evidence.verifier import VerificationSpine

    page = "Acme supplies chairs. Acme public pricing S$129 per unit. Request a quote."
    ledger, cand, claim_ref = _spine_candidate(page, 129.0)
    cv = VerificationSpine(ledger).verify_candidate(cand)
    price = next(c for c in cv.claims if c.field == "price")
    assert price.grounding == "grounded" and price.decision == "proceed"
    # acme.sg is an unlisted host with procurement signals -> business tier,
    # exact span present -> stays moderate.
    assert price.grade == "moderate"
    assert cv.decision == "proceed"
    assert ledger.get(claim_ref.ledger_id).metadata.get("grounding") == "grounded"


def test_spine_emits_contradicted_replan_when_source_disagrees():
    from spider_qwen.evidence.verifier import VerificationSpine

    page = "Acme supplies chairs. Acme public pricing S$129 per unit. Request a quote."
    ledger, cand, _ = _spine_candidate(page, 999.0)
    cv = VerificationSpine(ledger).verify_candidate(cand)
    price = next(c for c in cv.claims if c.field == "price")
    assert price.verified is False
    assert price.grounding == "contradicted" and price.decision == "replan"
    assert cv.decision == "replan"
    assert cv.verified is False
