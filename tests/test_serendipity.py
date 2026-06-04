"""T-1.1: serendipity 4-slot output schema + scoring (rank-based scaffold).

Run results are reshaped into {primary_answer, s1_substitutes,
s2_long_tail_sources, s3_risk_signals, evidence_refs, serendipity_score}.
Phase 1 maps s1 to ranks 2-4 and s2 to rank 5+; true S1/S2 classification
arrives in later graph/bandit phases.
"""

from __future__ import annotations

import asyncio

from spider_qwen.agent.controller import Controller
from spider_qwen.evidence.models import EvidenceRef, utc_now_iso
from spider_qwen.modes.contracts import PricingStatus, ServiceCandidate
from spider_qwen.ranking.serendipity import SerendipityResult, build_serendipity_result
from spider_qwen.tools.fetch_service import MockFetchProvider
from spider_qwen.tools.search_service import MockSearchProvider

SLOTS = ("primary_answer", "s1_substitutes", "s2_long_tail_sources",
         "s3_risk_signals", "evidence_refs", "serendipity_score")


def _ref(tag: str) -> EvidenceRef:
    return EvidenceRef(ledger_id=f"ev_{tag}", url=f"https://{tag}.example/x",
                       snippet_hash="h", retrieved_at=utc_now_iso())


def _cand(name: str, score: float, geo: float = 20.0) -> ServiceCandidate:
    c = ServiceCandidate(
        vendor_name=name, website=f"https://{name}.example.sg", country="Singapore",
        geo_score=geo, evidence_refs=[_ref(name)], evidence_completeness=1.0,
        service_match_evidence=True, service_match_score=1.0,
    )
    c.score = score
    return c


def test_serendipity_schema_has_four_slots():
    res = build_serendipity_result([_cand("a", 90), _cand("b", 80)],
                                   mode="service_quote_required")
    assert isinstance(res, SerendipityResult)
    d = res.model_dump()
    for slot in SLOTS:
        assert slot in d


def test_serendipity_scores_in_range():
    cands = [_cand(f"v{i}", 90 - i * 10, geo=g)
             for i, g in enumerate([20.0, 12.0, 6.0, 0.0, 0.0])]
    res = build_serendipity_result(cands, mode="service_quote_required")
    assert 0.0 <= res.serendipity_score <= 1.0
    for item in res.s1_substitutes + res.s2_long_tail_sources:
        assert 0.0 <= item.serendipity_score <= 1.0
        assert 0.0 <= item.relevance <= 1.0
        assert 0.0 <= item.novelty <= 1.0
        assert 0.0 <= item.unexpectedness <= 1.0


def test_primary_is_highest_ranked():
    res = build_serendipity_result([_cand("second", 50), _cand("top", 95)],
                                   mode="service_quote_required")
    assert res.primary_answer is not None
    assert res.primary_answer["vendor_name"] == "top"


def test_substitutes_are_ranks_2_to_4():
    """Scaffold: s1_substitutes are ranked positions 2-4, not graph-derived substitutes."""
    cands = [_cand(f"v{i}", 100 - i) for i in range(6)]
    res = build_serendipity_result(cands, mode="service_quote_required")
    assert [s.candidate["vendor_name"] for s in res.s1_substitutes] == ["v1", "v2", "v3"]


def test_long_tail_below_rank_5():
    cands = [_cand(f"v{i}", 100 - i) for i in range(7)]
    res = build_serendipity_result(cands, mode="service_quote_required")
    assert [s.candidate["vendor_name"] for s in res.s2_long_tail_sources] == ["v4", "v5", "v6"]


def test_evidence_refs_aggregated_across_slots():
    cands = [_cand(f"v{i}", 100 - i) for i in range(4)]
    res = build_serendipity_result(cands, mode="service_quote_required")
    assert {r.ledger_id for r in res.evidence_refs} == {"ev_v0", "ev_v1", "ev_v2", "ev_v3"}


def test_risk_signal_for_conflicting_pricing():
    bad = _cand("conf", 50)
    bad.pricing_status = PricingStatus.CONFLICTING
    res = build_serendipity_result([_cand("ok", 90), bad], mode="service_quote_required")
    types = {s.signal_type for s in res.s3_risk_signals}
    assert "pricing_conflict" in types
    sig = next(s for s in res.s3_risk_signals if s.signal_type == "pricing_conflict")
    assert sig.evidence_refs  # risk signal carries evidence


def test_extra_risk_signals_are_merged():
    from spider_qwen.ranking.serendipity import RiskSignal

    extra = RiskSignal(signal_type="eol_pcn", severity="high", description="EOL detected")
    res = build_serendipity_result([_cand("ok", 90)], mode="service_quote_required",
                                   extra_risk_signals=[extra])
    assert any(s.signal_type == "eol_pcn" for s in res.s3_risk_signals)


def test_empty_ranked_yields_empty_result():
    res = build_serendipity_result([], mode="service_quote_required")
    assert res.primary_answer is None
    assert res.s1_substitutes == []
    assert res.serendipity_score == 0.0


def test_controller_run_attaches_serendipity():
    controller = Controller(search_provider=MockSearchProvider(),
                            fetch_provider=MockFetchProvider(), state_dir=None, persist=False)
    result = asyncio.run(controller.run("office cleaning Singapore", mode="auto"))
    assert result.serendipity is not None
    s = result.serendipity
    for slot in SLOTS:
        assert slot in s
    assert s["primary_answer"] is not None
    assert 0.0 <= s["serendipity_score"] <= 1.0
    # every slot item carries evidence (evidence-first invariant)
    for item in s["s1_substitutes"] + s["s2_long_tail_sources"]:
        assert item["candidate"]["evidence_refs"]
