"""Regression tests for the trust-seam review fixes.

Each test pins one repaired defect: numeric normalization merging digit runs,
cross-boundary substring grounding, boilerplate token overlap, empty-value
relation claims, the critical-only gate scalar, complementary citation
re-pointing, host-deduped corroboration, the LTT selective-risk gate, trust
assessment keying, and the RFQ grade floor.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from spider_qwen.evidence.ledger import EvidenceLedger
from spider_qwen.evidence.verifier import VerificationSpine
from spider_qwen.verification.conformal import CalibrationExample, SelectiveRiskGate
from spider_qwen.verification.grade import grade_at_least
from spider_qwen.verification.grounding import detect_numeric_contradiction
from spider_qwen.verification.minicheck import MiniCheck, value_grounded


# --- minicheck normalization (digit runs, boundaries, boilerplate) ------------

def test_price_grounds_next_to_quantity_column():
    # Stripping whitespace+commas used to merge "S$1,299 50 pcs" into
    # "129950pcs": the real price failed verification and a fabricated
    # 129950 passed it.
    premise = "Unit price S$1,299 50 pcs minimum order"
    assert value_grounded("1299", premise) is True
    assert value_grounded("129950", premise) is False


def test_thousands_separator_still_normalizes():
    assert value_grounded("1299", "Price: S$1,299 per unit") is True


def test_numeric_contradiction_survives_adjacent_quantity():
    # The digit-run merge also corrupted contradiction detection: the claimed
    # value seemed absent, so the quantity column read as a competing price.
    premise = "Acme Pte Ltd unit price S$1,299 50 pcs minimum order."
    assert detect_numeric_contradiction("Acme Pte Ltd", "1299", premise) is False


def test_short_value_does_not_ground_across_token_boundaries():
    assert value_grounded("3M", "cable diameter 3 mm nominal") is False
    assert value_grounded("3M", "3M Singapore authorised distributor") is True


def test_vendor_name_cannot_verify_on_legal_boilerplate():
    # Generic tokens (pte/ltd/trading) must not carry the overlap: a wrong
    # vendor name used to verify against any page of legal boilerplate.
    res = MiniCheck().check(
        claim="The vendor is Zenith Trading Pte Ltd.",
        value="Zenith Trading Pte Ltd",
        evidence_span="Some Other Firm Pte Ltd offers trading services in Singapore.",
    )
    assert res.supported is False


def test_full_vendor_name_still_verifies():
    res = MiniCheck().check(
        claim="The vendor is Zenith Trading Pte Ltd.",
        value="Zenith Trading Pte Ltd",
        evidence_span="Zenith Trading Pte Ltd supplies industrial fasteners.",
    )
    assert res.supported is True


def test_empty_value_relation_claim_fails_closed():
    # An empty-valued quote channel (e.g. proposed by an untrusted model) must
    # not verify via its predicate-template words ("accepts quote requests via").
    res = MiniCheck().check(
        claim="Acme Pte Ltd accepts quote requests via .",
        value="",
        evidence_span="Acme Pte Ltd: request a quote via our sales team.",
        subject="Acme Pte Ltd",
    )
    assert res.supported is False
    assert res.method == "no_value"


# --- verification spine (gate scalar, re-pointing, corroboration) -------------

def _page(ledger: EvidenceLedger, url: str, text: str):
    return ledger.record(source_tool="tinyfish_fetch", url=url,
                         snippet=text[:80], text=text)


def _price_row(ledger: EvidenceLedger, page_ref, value: str):
    return ledger.record(
        source_tool="tinyfish_fetch", url=page_ref.url, snippet=value,
        metadata={"claim_id": "c_price", "field": "pricing",
                  "parent_ledger_id": page_ref.ledger_id},
    )


def _candidate(page_ref, price_row_ref, *, moq=None):
    return SimpleNamespace(
        vendor_name="Acme Pte Ltd", price=129.0, currency="SGD", unit="unit",
        moq=moq, pricing_status="EXACT_PRICE",
        evidence_refs=[page_ref, price_row_ref],
    )


def test_verifier_score_reads_critical_claims_only():
    # A weak NON-critical claim (MOQ absent from the page) must not drag the
    # scalar the emission gate reads below a flawless critical price claim.
    ledger = EvidenceLedger("run_seam", None)
    page = _page(ledger, "https://acme.example/pricing",
                 "Acme Pte Ltd unit price S$129 per unit for office chairs.")
    row = _price_row(ledger, page, "129")
    cv = VerificationSpine(ledger).verify_candidate(_candidate(page, row, moq="500"))
    moq = next(c for c in cv.claims if c.field == "moq")
    assert moq.verified is False and moq.critical is False
    assert cv.verified is True
    assert cv.verifier_score == 1.0  # min over critical claims, not all claims


def test_complementary_claim_is_repointed_to_corroborating_row():
    # GSAR "re-cite", actuated: when the cited page fails but another ledger
    # page grounds the claim, the corroborating ref joins the candidate's
    # citations and the claim row records the repair.
    ledger = EvidenceLedger("run_seam", None)
    cited = _page(ledger, "https://acme.example/about",
                  "Acme Pte Ltd is a family business founded by two engineers.")
    corro = _page(ledger, "https://directory.example/acme",
                  "Acme Pte Ltd lists a unit price of S$129 per unit.")
    row = _price_row(ledger, cited, "129")
    cand = _candidate(cited, row)
    cv = VerificationSpine(ledger).verify_candidate(cand)
    price = next(c for c in cv.claims if c.field == "price")
    assert price.grounding == "complementary"
    assert price.repointed_ledger_id == corro.ledger_id
    assert any(r.ledger_id == corro.ledger_id for r in cand.evidence_refs)
    assert ledger.get(row.ledger_id).metadata["repointed_to"] == corro.ledger_id


def test_corroboration_upgrade_ignores_same_host_repeats():
    # The vendor's own /about and /catalog repeating the price are ONE source:
    # no "+1 for 3 independent corroborating spans" from a single host.
    ledger = EvidenceLedger("run_seam", None)
    text = "Acme Pte Ltd unit price S$129 per unit."
    cited = _page(ledger, "https://acme.example/pricing", text)
    _page(ledger, "https://acme.example/about", text + " About our company.")
    _page(ledger, "https://acme.example/catalog", text + " Catalog of chairs.")
    row = _price_row(ledger, cited, "129")
    cv = VerificationSpine(ledger).verify_candidate(_candidate(cited, row))
    price = next(c for c in cv.claims if c.field == "price")
    assert price.grounding == "grounded"
    assert price.grade == "low"  # business start (moderate) -1 no exact span; no upgrade


def test_corroboration_upgrade_counts_distinct_hosts():
    ledger = EvidenceLedger("run_seam", None)
    text = "Acme Pte Ltd unit price S$129 per unit."
    cited = _page(ledger, "https://acme.example/pricing", text)
    _page(ledger, "https://directory.example/acme", text + " Verified supplier listing.")
    _page(ledger, "https://b2b.example/acme", text + " Registered exporter profile.")
    row = _price_row(ledger, cited, "129")
    cv = VerificationSpine(ledger).verify_candidate(_candidate(cited, row))
    price = next(c for c in cv.claims if c.field == "price")
    assert price.grade == "moderate"  # low + corroboration upgrade across hosts


# --- LTT selective-risk gate ---------------------------------------------------

def test_selective_gate_uncalibrated_never_claims_guarantee():
    gate = SelectiveRiskGate.fit([], alpha=0.1, delta=0.1)
    decision = gate.decide(0.99)
    assert decision.abstain is True and decision.calibrated is False


def test_selective_gate_refuses_below_ltt_floor():
    # 12 error-free examples calibrate the coverage abstainer but cannot
    # certify P(wrong|emitted) <= 0.1 at confidence 0.9 (the Bonferroni grid
    # needs >= 33 error-free emitted examples at some threshold).
    examples = [CalibrationExample(verifier_score=0.9, prediction_correct=True)
                for _ in range(12)]
    gate = SelectiveRiskGate.fit(examples, alpha=0.1, delta=0.1)
    assert gate.threshold is None
    assert any("insufficient calibration data" in r for r in gate.reasons)


def test_selective_gate_certifies_with_enough_error_free_mass():
    examples = [CalibrationExample(verifier_score=0.8 + 0.004 * i, prediction_correct=True)
                for i in range(40)]
    gate = SelectiveRiskGate.fit(examples, alpha=0.1, delta=0.1)
    assert gate.threshold is not None
    assert gate.calibration_emitted == 40 and gate.calibration_wrong == 0
    assert gate.decide(0.95).abstain is False
    assert gate.decide(0.4).abstain is True
    assert "P(wrong|emitted)" in gate.decide(0.95).rationale


def test_selective_gate_threshold_sits_above_observed_errors():
    # Errors concentrated at 0.55-0.64 poison the 0.5 grid point; the gate
    # certifies the next threshold up instead of averaging the errors away.
    good = [CalibrationExample(verifier_score=0.8 + 0.004 * i, prediction_correct=True)
            for i in range(40)]
    bad = [CalibrationExample(verifier_score=0.55 + 0.01 * i, prediction_correct=False)
           for i in range(10)]
    gate = SelectiveRiskGate.fit(good + bad, alpha=0.1, delta=0.1)
    assert gate.threshold == 0.75
    assert gate.calibration_wrong == 0  # no observed error at/above the threshold


# --- trust attribution and grade floor ------------------------------------------

def test_assessment_key_separates_same_name_vendors():
    from spider_qwen.agent.controller import Controller

    a = SimpleNamespace(vendor_name="Acme Pte Ltd", website="https://acme.sg")
    b = SimpleNamespace(vendor_name="Acme Pte Ltd", website="https://acme-clean.com.sg")
    assert Controller._assessment_key(a) != Controller._assessment_key(b)


def test_grade_at_least_ordering_fails_closed():
    assert grade_at_least("high", "low") is True
    assert grade_at_least("very_low", "very_low") is True
    assert grade_at_least("very_low", "low") is False
    assert grade_at_least("unheard_of", "low") is False  # unknown ranks lowest


def test_policy_grade_floor_defaults_advisory_and_fails_loud():
    from spider_qwen.agent.policy import Policy

    assert Policy({}).rfq_grade_floor == "very_low"
    assert Policy({"rfq": {"grade_floor": "moderate"}}).rfq_grade_floor == "moderate"
    with pytest.raises(ValueError, match="grade_floor"):
        _ = Policy({"rfq": {"grade_floor": "medium"}}).rfq_grade_floor
