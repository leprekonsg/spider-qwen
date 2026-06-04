"""Dempster-Shafer belief fusion over disputed facts."""

from __future__ import annotations

import math

from spider_qwen.evidence.belief import (
    YAGER_CONFLICT_THRESHOLD,
    BeliefMass,
    bpa,
    fuse,
    fuse_disputed_fact,
)
from spider_qwen.evidence.ledger import EvidenceLedger
from spider_qwen.evidence.models import DisputedAlternative
from spider_qwen.memory.semantic import SemanticFact


def test_bpa_clamps_reliability_and_splits_mass():
    m = bpa(0.9, supports=True)
    assert math.isclose(m.true_mass + m.false_mass + m.unknown_mass, 1.0)
    assert m.true_mass == 0.9 and m.false_mass == 0.0
    certain = bpa(1.0, supports=False)
    assert certain.false_mass == 0.99  # clamped below 1: Dempster stays defined


def test_two_agreeing_sources_raise_belief_above_either_alone():
    fused, k, rule = fuse([bpa(0.8, supports=True), bpa(0.7, supports=True)])
    assert rule == "dempster" and k == 0.0
    assert fused.true_mass > 0.8
    bel = fused.true_mass
    pl = fused.true_mass + fused.unknown_mass
    assert bel <= pl


def test_dempster_combination_matches_hand_computation():
    # m1 = (0.8 T, 0.2 U), m2 = (0.6 F, 0.4 U): K = 0.48,
    # T = 0.8*0.4/0.52, F = 0.6*0.2/0.52, U = 0.08/0.52
    fused, k, rule = fuse([bpa(0.8, supports=True), bpa(0.6, supports=False)])
    assert rule == "dempster"
    assert math.isclose(k, 0.48)
    assert math.isclose(fused.true_mass, 0.32 / 0.52, rel_tol=1e-9)
    assert math.isclose(fused.false_mass, 0.12 / 0.52, rel_tol=1e-9)
    assert math.isclose(fused.unknown_mass, 0.08 / 0.52, rel_tol=1e-9)


def test_high_conflict_switches_to_yager_and_surfaces_unknown():
    # Two near-certain contradicting sources: Dempster would renormalize the
    # conflict away; Yager must surface it as unknown mass instead.
    fused, k, rule = fuse([bpa(0.99, supports=True), bpa(0.99, supports=False)])
    assert rule == "yager"
    assert k > YAGER_CONFLICT_THRESHOLD
    assert fused.unknown_mass > 0.9  # the conflict became epistemic uncertainty


def test_empty_and_single_inputs():
    fused, k, rule = fuse([])
    assert rule == "single" and fused.unknown_mass == 1.0
    one = BeliefMass(true_mass=0.7, unknown_mass=0.3)
    fused, k, rule = fuse([one])
    assert rule == "single" and fused == one


def _disputed_fact(ledger: EvidenceLedger | None = None) -> SemanticFact:
    refs_a = refs_b = []
    if ledger is not None:
        # Manufacturer page (reliability 0.99) vs marketplace listing (0.2).
        refs_a = [ledger.record(source_tool="mock", url="https://ti.com/quote",
                                snippet="price SGD 120")]
        refs_b = [ledger.record(source_tool="mock", url="https://alibaba.com/item",
                                snippet="price SGD 90")]
    return SemanticFact(
        entity_type="vendor", entity_name="Acme", field="price", value="120",
        confidence=0.9, status="disputed",
        evidence_refs=refs_a,
        disputed_alternatives=[
            DisputedAlternative(value="90", confidence=0.6, evidence_refs=refs_b),
        ],
    )


def test_fuse_disputed_fact_weights_sides_by_ledger_reliability():
    ledger = EvidenceLedger("run_belief")
    fact = _disputed_fact(ledger)
    intervals = fuse_disputed_fact(fact, ledger)
    assert [i.value for i in intervals] == ["120", "90"]
    mfr, market = intervals
    # The manufacturer-backed value dominates the marketplace-backed one.
    assert mfr.belief > market.belief
    assert mfr.belief <= mfr.plausibility
    assert market.belief <= market.plausibility
    assert mfr.supporting_sources == 1 and mfr.contradicting_sources == 1


def test_fuse_disputed_fact_without_ledger_falls_back_to_confidence():
    intervals = fuse_disputed_fact(_disputed_fact())
    assert len(intervals) == 2
    assert intervals[0].value == "120"  # higher confidence side wins
    assert all(0.0 <= i.belief <= i.plausibility <= 1.0 for i in intervals)


def test_undisputed_fact_returns_single_supported_interval():
    fact = SemanticFact(entity_type="vendor", entity_name="Acme", field="email",
                        value="sales@acme.sg", confidence=0.8)
    (interval,) = fuse_disputed_fact(fact)
    assert interval.value == "sales@acme.sg"
    assert interval.contradicting_sources == 0
    assert interval.belief > 0.0
    assert interval.uncertainty >= 0.0
