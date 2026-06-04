"""T-5.4 (core): proactive S3 signal bundle.

In scope: (a) datasheet deprecation-diff (a lifecycle worsening / new PCN between
two snapshots is a leading risk indicator) and (d) proactive-watch (an NRND/EOL
part auto-surfaces a deprecation signal + substitute alternates pulled from the
graph, without being asked). The OOD monitor (b) and counterfactual-BOM (c) were
demoted to NICE in the v2 amendment and are intentionally not implemented here.
"""

from __future__ import annotations

from spider_qwen.graph.store import GraphStore
from spider_qwen.serendipity.legacy_ocr_miner import ingest_legacy_text
from spider_qwen.serendipity.signals import (
    detect_lifecycle,
    diff_datasheet,
    proactive_watch,
)


def test_detect_lifecycle_returns_most_severe_status():
    assert detect_lifecycle("Status: Active. Recommended for new designs.") == "active"
    assert detect_lifecycle("This part is NRND (not recommended for new designs).") == "nrnd"
    assert detect_lifecycle("Product is End of Life / obsolete.") == "eol"
    assert detect_lifecycle("Preliminary datasheet, advance information.") == "preliminary"
    assert detect_lifecycle("") == "unknown"
    # Most severe wins when several markers co-occur.
    assert detect_lifecycle("NRND part; active alternates exist.") == "nrnd"


def test_diff_datasheet_flags_active_to_nrnd():
    old = "LM358 lifecycle: Active. Recommended for new designs."
    new = "LM358 lifecycle: NRND. Not recommended for new designs."
    sig = diff_datasheet(old, new, mpn="LM358")
    assert sig is not None
    assert sig.signal_type == "lifecycle_deprecation"
    assert sig.severity in {"medium", "high"}
    assert sig.entity == "LM358"


def test_detect_lifecycle_ignores_tokens_inside_words_and_part_numbers():
    # Word-boundary matching: lifecycle tokens embedded in MPNs / other words must
    # not flip the state (these are the false-positive/negative cases).
    assert detect_lifecycle("MX25L1606ENRND-10G flash. Active, recommended for new designs.") == "active"
    assert detect_lifecycle("C5017LTBG3 op-amp. In production.") == "active"
    assert detect_lifecycle("The inactive-low pin is asserted.") == "unknown"  # 'inactive' != active


def test_detect_lifecycle_accepts_eol_abbreviation():
    assert detect_lifecycle("This part is EOL as of 2024.") == "eol"


def test_has_pcn_requires_id_and_ignores_substrings():
    # EPCN (a chip) and "PDN design" (power delivery network) are not change notices.
    assert diff_datasheet("EPCN bus rev A.", "EPCN bus rev B. PDN design updated.", mpn="X") is None
    # A real PCN reference (with an id) is detected.
    assert diff_datasheet("LM358 rev A.", "LM358 rev B. See PCN-12345.", mpn="LM358") is not None


def test_diff_datasheet_flags_second_source_removed():
    old = "LM358 available from supplier A and supplier B."
    new = "LM358 available from supplier A only. Second source removed."
    sig = diff_datasheet(old, new, mpn="LM358")
    assert sig is not None and sig.entity == "LM358"


def test_diff_datasheet_no_worsening_returns_none():
    old = "LM358 lifecycle: Active. Recommended for new designs."
    new = "LM358 lifecycle: Active. In production."
    assert diff_datasheet(old, new, mpn="LM358") is None


def test_diff_datasheet_flags_new_pcn():
    old = "LM358 datasheet rev A."
    new = "LM358 datasheet rev B. Product Change Notification PCN-12345 issued."
    sig = diff_datasheet(old, new, mpn="LM358")
    assert sig is not None
    assert sig.entity == "LM358"


def _graph_with_alternates() -> GraphStore:
    store = GraphStore()
    # 3 substitute cross-references for LM358, each ledger-backed (ev1).
    ingest_legacy_text(store, "LM358 TLC272\nLM358 NE5532\nLM358 MC1458\n", evidence_claim_id="ev1")
    return store


def test_proactive_watch_nrnd_surfaces_signal_and_three_alternates():
    store = _graph_with_alternates()
    text = "LM358 dual op-amp. Lifecycle status: NRND (not recommended for new designs)."
    res = proactive_watch("LM358", text, store)

    assert res.triggered is True
    assert res.lifecycle_status == "nrnd"
    assert res.risk_signal is not None and res.risk_signal.severity in {"medium", "high"}
    assert len(res.alternates) >= 3  # >=3 alternates surfaced without being asked
    for alt in res.alternates:
        assert alt.relation == "CROSS_REFERENCE"
        assert alt.evidence_claim_id == "ev1"  # alternates are evidence-backed
    assert {a.mpn for a in res.alternates} == {"TLC272", "NE5532", "MC1458"}


def test_proactive_watch_active_part_does_not_trigger():
    store = _graph_with_alternates()
    res = proactive_watch("LM358", "LM358 active, recommended for new designs.", store)
    assert res.triggered is False
    assert res.risk_signal is None
    assert res.alternates == []


def test_proactive_watch_active_part_with_pcn_triggers_pcn_alert():
    store = _graph_with_alternates()
    text = "LM358 active, in production. Product Change Notification PCN-9 issued."
    res = proactive_watch("LM358", text, store)
    assert res.triggered is True
    assert res.risk_signal is not None and res.risk_signal.signal_type == "pcn_alert"
    assert len(res.alternates) >= 3


def test_proactive_watch_nrnd_with_no_graph_alternates_returns_empty():
    # An at-risk part with no substitute edges triggers the signal but fabricates nothing.
    store = GraphStore()
    res = proactive_watch("LM358", "LM358 is NRND.", store)
    assert res.triggered is True
    assert res.risk_signal is not None
    assert res.alternates == []
