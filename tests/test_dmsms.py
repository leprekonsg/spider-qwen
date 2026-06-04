"""T-6.1: DMSMS SD-22 six-strategy ladder + LTB calculator + counterfeit badge.

Acceptance: an EOL part returns a structured 6-rung mitigation ladder (not a
single quote); a broker on the ERAI fixture list renders a red counterfeit badge.
"""

from __future__ import annotations

from spider_qwen.evidence.models import EvidenceRef
from spider_qwen.serendipity.dmsms import (
    DMSMS_LADDER_ORDER,
    build_dmsms_ladder,
    counterfeit_badge,
    ltb_quantity,
)


def _ref() -> EvidenceRef:
    return EvidenceRef(ledger_id="ev1", url="legacy://book/p1", snippet_hash="h", retrieved_at="t")


def test_ltb_quantity_covers_service_life_plus_safety():
    rec = ltb_quantity(annual_demand=1000, service_life_years=5)
    assert rec.quantity >= 5000          # at least demand over the remaining service life
    assert rec.safety_stock > 0
    assert rec.rationale


def test_ltb_quantity_is_cost_sensitive():
    # Shortage cost dominating holding/disposal -> buy more safety stock.
    cheap_to_hold = ltb_quantity(1000, 5, holding_cost=1, shortage_cost=50)
    costly_to_hold = ltb_quantity(1000, 5, holding_cost=50, shortage_cost=1)
    assert cheap_to_hold.quantity > costly_to_hold.quantity


def test_build_dmsms_ladder_has_six_rungs_in_priority_order():
    ladder = build_dmsms_ladder("DF13-6P-1.25DSA", lifecycle_status="eol")
    assert [r.strategy for r in ladder.rungs] == list(DMSMS_LADDER_ORDER)
    assert len(ladder.rungs) == 6
    assert [r.rank for r in ladder.rungs] == [1, 2, 3, 4, 5, 6]


def test_ladder_marks_applicable_rungs_from_context():
    ladder = build_dmsms_ladder(
        "LM358", lifecycle_status="nrnd",
        on_hand=200, alternates=["TLC272", "NE5532"],
        alt_sources=["Rochester Electronics"], ltb=ltb_quantity(500, 4),
        evidence_refs=[_ref()],
    )
    by = {r.strategy: r for r in ladder.rungs}
    assert by["existing_stock"].applicable          # on_hand / LTB present
    assert by["substitute"].applicable and "TLC272" in by["substitute"].candidates
    assert by["alternate_source"].applicable and "Rochester Electronics" in by["alternate_source"].candidates
    assert by["redesign"].applicable and by["emulation"].applicable  # always-available fallbacks
    assert ladder.ltb is not None and ladder.ltb.quantity > 0
    assert ladder.evidence_refs and ladder.evidence_refs[0].ledger_id == "ev1"


def test_counterfeit_badge_red_on_erai_hit():
    badge = counterfeit_badge("Shady Components Ltd", source_type="broker",
                              erai_list=["shady components ltd"])
    assert badge.risk_level == "red"
    assert badge.erai_flagged is True
    assert badge.far_citation == "FAR 52.246-26"


def test_counterfeit_badge_red_on_gidep_advisory():
    badge = counterfeit_badge("Risky Parts Inc", source_type="broker",
                              gidep_list=["risky parts inc"])
    assert badge.risk_level == "red"
    assert badge.gidep_flagged is True


def test_counterfeit_badge_amber_for_unscreened_broker():
    badge = counterfeit_badge("Generic Broker Co", source_type="broker")
    assert badge.risk_level == "amber"      # unauthorized source, not on a bad list
    assert badge.as6081_status and badge.as6171_status


def test_counterfeit_badge_green_for_authorized_distributor():
    badge = counterfeit_badge("Mouser", source_type="authorised_distributor")
    assert badge.risk_level == "green"
    assert badge.erai_flagged is False and badge.gidep_flagged is False
