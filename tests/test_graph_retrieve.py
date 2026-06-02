"""T-3.2: HippoRAG Personalized PageRank multi-hop retrieval over the LPG.

Acceptance: a multi-hop substitute query returns the successor chain + a human-
readable relation path; PPR beats a flat-vector baseline on recall@5 over a
10-query fixture.
"""

from __future__ import annotations

from spider_qwen.agent.controller import Controller
from spider_qwen.evidence.ledger import EvidenceLedger
from spider_qwen.graph.retrieve import (
    GraphRetriever,
    flat_vector_rank,
    personalized_pagerank,
)
from spider_qwen.graph.schema import part_key
from spider_qwen.graph.store import GraphStore
from spider_qwen.tools.fetch_service import MockFetchProvider
from spider_qwen.tools.search_service import MockSearchProvider


def _chain_store() -> GraphStore:
    store = GraphStore()
    for nid in ("a", "b", "c"):
        store.upsert_node(nid, "Part")
    store.add_edge("a", "b", "SUPERSEDED_BY", confidence=0.9, reliability=0.9, evidence_claim_id="e1")
    store.add_edge("b", "c", "CROSS_REFERENCE", confidence=0.9, reliability=0.9, evidence_claim_id="e2")
    store.upsert_node("x", "Part")
    store.add_edge("x", "x_ref", "CROSS_REFERENCE", confidence=0.9, reliability=0.9, evidence_claim_id="e3")
    return store


def test_ppr_scores_connected_above_unconnected():
    store = _chain_store()
    scores = personalized_pagerank(GraphRetriever(store)._adjacency(), ["a"])
    assert scores["b"] > 0 and scores["c"] > 0
    assert scores.get("x", 0.0) == 0.0  # not reachable from seed 'a'
    assert scores["b"] > scores["c"]    # closer hop ranks higher


def test_retrieve_returns_successor_chain_with_path():
    store = _chain_store()
    store.upsert_node("part:atmega48", "Part", {"surface": "ATMEGA48"})
    store.upsert_node("part:atmega48a", "Part", {"surface": "ATMEGA48A"})
    store.add_edge("part:atmega48", "part:atmega48a", "SUPERSEDED_BY",
                   confidence=0.9, reliability=0.9, evidence_claim_id="e9")
    res = GraphRetriever(store).retrieve("substitute for ATMEGA48")
    assert res.seeds == ["part:atmega48"]
    succ = [r for r in res.results if r.id == "part:atmega48a"]
    assert succ
    assert "SUPERSEDED_BY" in succ[0].path
    assert succ[0].depth == 1


def _recall_fixture():
    store = GraphStore()
    store.upsert_node("part:dead", "Part", {"surface": "DEAD000"})
    queries, golds = [], []
    for i in range(10):
        p, s, d = f"ABC1{i}0", f"XYZ9{i}0", f"ABC1{i}5"
        pid, sid, did = part_key(p), part_key(s), part_key(d)
        store.upsert_node(pid, "Part", {"surface": p})
        store.upsert_node(sid, "Part", {"surface": s})
        store.upsert_node(did, "Part", {"surface": d})
        store.add_edge(pid, sid, "SUPERSEDED_BY", confidence=0.9, reliability=0.9, evidence_claim_id=f"e{i}")
        store.add_edge(did, "part:dead", "CROSS_REFERENCE", confidence=0.9, reliability=0.9, evidence_claim_id=f"d{i}")
        queries.append(f"alternative for {p}")
        golds.append(sid)
    return store, queries, golds


def _recall_at_k(rankings: list[list[str]], golds: list[str], k: int) -> float:
    return sum(1 for r, g in zip(rankings, golds) if g in r[:k]) / len(golds)


def test_ppr_beats_flat_vector_baseline_recall_at_5():
    store, queries, golds = _recall_fixture()
    retr = GraphRetriever(store)
    ppr = [[n.id for n in retr.retrieve(q, top_k=5).results] for q in queries]
    flat = [flat_vector_rank(store, q, top_k=5) for q in queries]
    ppr_recall = _recall_at_k(ppr, golds, 5)
    flat_recall = _recall_at_k(flat, golds, 5)
    assert ppr_recall > flat_recall, f"ppr={ppr_recall} flat={flat_recall}"
    assert ppr_recall >= 0.9


def test_controller_graph_retrieve_over_ledger():
    controller = Controller(search_provider=MockSearchProvider(),
                            fetch_provider=MockFetchProvider(), state_dir=None, persist=False)
    ledger = EvidenceLedger("run_graph")
    ledger.record(
        source_tool="tinyfish_fetch", url="https://microchip.com/atmega48",
        snippet="ATMEGA48", confidence=0.6,
        text="The ATMEGA48 was acquired by Microchip. The ATMEGA48 is superseded by the ATMEGA48A.",
    )
    res = controller.graph_retrieve("substitute for ATMEGA48", ledger)
    assert "part:atmega48a" in {r.id for r in res.results}
