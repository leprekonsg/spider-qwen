"""T-3.1: supplier-part property graph (SQLite LPG).

Acceptance: ingesting fixture datasheet text builds a graph where the recursive-
CTE traversal resolves "ATMEGA48 -> (ACQUIRED_BY Microchip), (SUPERSEDED_BY
ATMEGA48A)" within <=2 hops.
"""

from __future__ import annotations

import pytest

from spider_qwen.evidence.graph import render_property_graph
from spider_qwen.graph.extract import extract_triples, ingest_text
from spider_qwen.graph.store import GraphStore


# --- store -----------------------------------------------------------------

def _edge(store, src, dst, rel, cid):
    store.upsert_node(src, "Part")
    store.upsert_node(dst, "Part")
    store.add_edge(src, dst, rel, confidence=0.9, reliability=0.9, evidence_claim_id=cid)


def test_upsert_node_merges_props():
    store = GraphStore()
    store.upsert_node("part:x", "Part", {"a": 1})
    store.upsert_node("part:x", "Part", {"b": 2})
    assert store.get_node("part:x")["props"] == {"a": 1, "b": 2}


def test_traverse_resolves_two_hops():
    store = GraphStore()
    _edge(store, "a", "b", "ACQUIRED_BY", "ev1")
    _edge(store, "b", "c", "SUPERSEDED_BY", "ev2")
    res = store.traverse("a", rels=["ACQUIRED_BY", "SUPERSEDED_BY"], max_depth=2)
    ids = {r["id"] for r in res}
    assert ids == {"b", "c"}
    two_hop = [r for r in res if r["depth"] == 2]
    assert two_hop and two_hop[0]["id"] == "c"


def test_traverse_respects_max_depth():
    store = GraphStore()
    _edge(store, "a", "b", "ACQUIRED_BY", "ev1")
    _edge(store, "b", "c", "SUPERSEDED_BY", "ev2")
    res = store.traverse("a", rels=["ACQUIRED_BY", "SUPERSEDED_BY"], max_depth=1)
    assert {r["id"] for r in res} == {"b"}


def test_traverse_filters_relations():
    store = GraphStore()
    _edge(store, "a", "b", "ACQUIRED_BY", "ev1")
    assert store.traverse("a", rels=["SUPERSEDED_BY"], max_depth=2) == []


def test_edge_primary_key_is_per_asserting_claim():
    store = GraphStore()
    store.upsert_node("a", "Part")
    store.upsert_node("b", "Part")
    store.add_edge("a", "b", "ACQUIRED_BY", confidence=0.9, reliability=0.9, evidence_claim_id="ev1")
    store.add_edge("a", "b", "ACQUIRED_BY", confidence=0.5, reliability=0.9, evidence_claim_id="ev1")  # update
    store.add_edge("a", "b", "ACQUIRED_BY", confidence=0.7, reliability=0.9, evidence_claim_id="ev2")  # new claim
    assert store.edge_count() == 2


def test_add_edge_requires_evidence_claim_id():
    store = GraphStore()
    store.upsert_node("a", "Part")
    store.upsert_node("b", "Part")
    with pytest.raises(ValueError):
        store.add_edge("a", "b", "ACQUIRED_BY", confidence=0.9, reliability=0.9, evidence_claim_id="")


# --- extraction ------------------------------------------------------------

def test_extract_acquired_and_superseded_triples():
    triples = extract_triples(
        "The ATMEGA48 was acquired by Microchip. The ATMEGA48 is superseded by the ATMEGA48A."
    )
    rels = {(t.subject_id, t.rel, t.object_id) for t in triples}
    assert ("part:atmega48", "ACQUIRED_BY", "mfr:microchip") in rels
    assert ("part:atmega48", "SUPERSEDED_BY", "part:atmega48a") in rels


def test_extract_ignores_unknown_entities():
    assert extract_triples("Widgets were acquired by gadgets.") == []


def test_ingest_builds_graph_with_evidence_backed_edges():
    store = GraphStore()
    valid_from = "2026-06-01T00:00:00+00:00"
    added = ingest_text(store, "The ATMEGA48 was acquired by Microchip.",
                        evidence_claim_id="ev_page", reliability=0.95,
                        valid_from=valid_from)
    assert added
    assert store.get_node("part:atmega48")["type"] == "Part"
    nbrs = store.neighbors("part:atmega48", rels=["ACQUIRED_BY"])
    assert nbrs[0]["dst"] == "mfr:microchip"
    assert nbrs[0]["evidence_claim_id"] == "ev_page"
    assert nbrs[0]["reliability"] == 0.95
    version = store.versions("part:atmega48", "mfr:microchip", "ACQUIRED_BY")[0]
    assert version["event_ts"] == valid_from
    assert "ATMEGA48 was acquired by Microchip" in version["props"]["sentence"]


# --- acceptance ------------------------------------------------------------

def test_acceptance_supersession_chain_resolves_within_two_hops():
    store = GraphStore()
    fixture = (
        "The ATMEGA48 was acquired by Microchip. "
        "The ATMEGA48 is superseded by the ATMEGA48A. "
        "The ATMEGA48 cross-references the PIC16F88."
    )
    ingest_text(store, fixture, evidence_claim_id="ev_datasheet")
    paths = store.traverse(
        "part:atmega48", rels=["ACQUIRED_BY", "SUPERSEDED_BY", "CROSS_REFERENCE"], max_depth=2
    )
    ids = {p["id"] for p in paths}
    assert "mfr:microchip" in ids
    assert "part:atmega48a" in ids
    assert all(p["depth"] <= 2 for p in paths)


def test_render_property_graph_mermaid():
    store = GraphStore()
    ingest_text(store, "The ATMEGA48 was acquired by Microchip.", evidence_claim_id="ev1")
    mmd = render_property_graph(store)
    assert mmd.startswith("graph LR")
    assert "ACQUIRED_BY" in mmd
    assert "part_atmega48" in mmd
