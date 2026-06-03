"""T-4.3: bi-temporal facts on the SQLite edge table (Zep/Graphiti dropped).

Reuses event_ts (valid-from) / ingest_ts (recorded-at) + a new valid_to column.
Supersession is append-only: a newer fact closes the prior row's valid_to but
never deletes it; "current" is the open row with the max valid_from.
"""

from __future__ import annotations

from spider_qwen.graph.store import GraphStore
from spider_qwen.memory.temporal import BiTemporalGraph, TemporalFact

OLD = "2026-05-01T00:00:00+00:00"
NEW = "2026-06-01T00:00:00+00:00"


def _graph() -> BiTemporalGraph:
    store = GraphStore(":memory:")
    store.upsert_node("dist:acme", "Distributor")
    store.upsert_node("part:ne5532", "Part")
    g = BiTemporalGraph(store)
    g.record("dist:acme", "part:ne5532", "STOCKED_AT", props={"price": 10.0},
             confidence=0.9, reliability=0.95, evidence_claim_id="ev_old", valid_from=OLD)
    g.record("dist:acme", "part:ne5532", "STOCKED_AT", props={"price": 12.0},
             confidence=0.9, reliability=0.95, evidence_claim_id="ev_new", valid_from=NEW)
    return g


def test_price_history_returns_both_rows_with_timestamps():
    g = _graph()
    hist = g.history("dist:acme", "part:ne5532", "STOCKED_AT")
    assert len(hist) == 2
    assert [h.props["price"] for h in hist] == [10.0, 12.0]  # ordered by valid_from
    assert all(isinstance(h, TemporalFact) and h.valid_from for h in hist)


def test_current_is_latest_and_prior_is_retained_not_deleted():
    g = _graph()
    current = g.current("dist:acme", "part:ne5532", "STOCKED_AT")
    assert current.props["price"] == 12.0
    assert current.valid_to is None

    hist = g.history("dist:acme", "part:ne5532", "STOCKED_AT")
    prior = next(h for h in hist if h.evidence_claim_id == "ev_old")
    assert prior.valid_to is not None  # superseded, not deleted
    assert g.store.edge_count() == 2  # both rows persist


def test_as_of_returns_the_fact_valid_at_a_past_instant():
    g = _graph()
    past = g.as_of("dist:acme", "part:ne5532", "STOCKED_AT", "2026-05-15T00:00:00+00:00")
    assert past is not None and past.props["price"] == 10.0
    now = g.as_of("dist:acme", "part:ne5532", "STOCKED_AT", "2026-06-15T00:00:00+00:00")
    assert now is not None and now.props["price"] == 12.0
    before_any = g.as_of("dist:acme", "part:ne5532", "STOCKED_AT", "2026-04-01T00:00:00+00:00")
    assert before_any is None


def test_current_view_excludes_superseded_rows():
    g = _graph()
    current_rows = g.store.current_edges()
    stocked = [r for r in current_rows if r["rel"] == "STOCKED_AT"]
    assert len(stocked) == 1
    assert stocked[0]["evidence_claim_id"] == "ev_new"


def test_superseded_edge_excluded_from_default_reads():
    """Supersession must take effect in the normal read paths, not just the
    explicit current_edges() view: edges(), neighbors(), and traverse() return
    only open (valid_to IS NULL) edges. Full history stays reachable via
    versions()."""
    g = _graph()

    assert all(e["evidence_claim_id"] != "ev_old" for e in g.store.edges())

    nbrs = g.store.neighbors("dist:acme", rels=["STOCKED_AT"])
    assert [n["evidence_claim_id"] for n in nbrs] == ["ev_new"]

    hops = g.store.traverse("dist:acme", rels=["STOCKED_AT"], max_depth=1)
    assert len(hops) == 1  # only the open edge is traversed, not the superseded one

    # History is not lost -- both versions remain queryable.
    assert len(g.store.versions("dist:acme", "part:ne5532", "STOCKED_AT")) == 2
