"""T-3.3: GRAM-lite Serendipity Mode.

Acceptance: substitute & revalidation modes invoke Serendipity Mode; the result
shows the recursion tree; cost/latency stay within caps (<=1 Max + <=25 flash +
<=45 fetch per top-level query).
"""

from __future__ import annotations

from spider_qwen.modes.contracts import ProcurementMode
from spider_qwen.modes.router import ModeRouter
from spider_qwen.serendipity.gram_lite import (
    CATEGORIES,
    Caps,
    run_serendipity,
    sample_trajectories,
)


def test_sample_trajectories_covers_all_five_categories():
    trajs = sample_trajectories("16-pin SMT op-amp Texas Instruments NE5532", mode="product_exact_price")
    assert [t.category for t in trajs] == list(CATEGORIES)
    assert all(t.query for t in trajs)
    assert any("singapore" in t.query.lower() for t in trajs)  # SEA-local trajectory


def test_run_serendipity_builds_recursion_tree():
    res = run_serendipity("NE5532 substitute", mode="product_exact_price", ts="t0")
    assert res.edges
    root = res.edges[0].parent_sha
    depth1 = [e for e in res.edges if e.depth == 1]
    assert depth1 and all(e.parent_sha == root for e in depth1)
    assert all(e.ts == "t0" for e in res.edges)
    assert any(e.depth == 3 for e in res.edges)  # multi-hop to D_MAX


def test_run_serendipity_respects_caps():
    res = run_serendipity("NE5532 substitute", mode="product_exact_price", ts="t0")
    assert res.cost["max_calls"] <= 1
    assert res.cost["flash"] <= 25
    assert res.cost["fetch"] <= 45
    assert res.within_caps is True


def test_per_hop_confidence_decay():
    res = run_serendipity("NE5532 substitute", mode="product_exact_price", ts="t0")
    by_depth = {e.depth: e.confidence for e in res.edges}
    assert by_depth[1] == 0.85
    assert by_depth[2] == round(0.85 ** 2, 4)
    assert by_depth[3] == round(0.85 ** 3, 4)


def test_verification_fanout_routes_dispute_to_handler():
    def verify_fn(_query, lens):
        return {"refetch": "active", "competing_vendor": "NRND", "wayback": "active"}[lens]

    captured = []
    res = run_serendipity(
        "NE5532 substitute", mode="product_exact_price", verify_fn=verify_fn,
        disputed_handler=captured.append, caps=Caps(max_calls=1, flash=5, fetch=20),
        d_max=1, width=2, ts="t0",
    )
    assert res.verifications
    assert res.disputed and res.disputed[0].agree is False
    assert len(captured) == len(res.disputed)
    assert res.within_caps is True


def test_no_dispute_when_verifiers_agree():
    res = run_serendipity(
        "NE5532 substitute", mode="product_exact_price",
        verify_fn=lambda q, lens: "active", caps=Caps(1, 5, 20), d_max=1, width=2, ts="t0",
    )
    assert res.verifications
    assert res.disputed == []


def test_router_substitute_and_revalidation_invoke_serendipity():
    r = ModeRouter()
    assert r.route(ProcurementMode.PRODUCT_EXACT_PRICE).serendipity_mode is True
    assert r.route(ProcurementMode.REVALIDATION).serendipity_mode is True
    assert r.route(ProcurementMode.SERVICE_QUOTE_REQUIRED).serendipity_mode is False
    assert r.route(ProcurementMode.CONTACT_ENRICHMENT_ONLY).serendipity_mode is False
