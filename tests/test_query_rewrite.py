"""T-1.2: Step-Back + HyDE + Query2Doc query expansion with obsolescence vocab.

Deterministic and offline (no API): the golden test must pass with no keys.
"""

from __future__ import annotations

import re

from spider_qwen.ranking.geo_strategy import build_query_templates
from spider_qwen.serendipity.query_rewrite import (
    OBSOLESCENCE_VOCAB,
    SearchQuery,
    expand_query,
    merge_gather_queries,
)

_MPN_RE = re.compile(r"[A-Za-z]{2,}\d+")
_BROKERS = ("rochester", "lansdale", "oemsecrets", "octopart", "avnet", "digikey", "mouser")


def test_expand_returns_search_query_objects():
    qs = expand_query("office cleaning Singapore")
    assert qs and all(isinstance(q, SearchQuery) for q in qs)
    assert all(q.text and q.kind for q in qs)


def test_expand_distinct_and_length_capped():
    qs = expand_query("16-pin SMT op-amp TI used in 90s ECUs")
    texts = [q.text for q in qs]
    assert len(set(texts)) == len(texts)
    assert all(len(t) <= 200 for t in texts)


def test_golden_obsolete_opamp_expansion():
    qs = expand_query("16-pin SMT op-amp TI used in 90s ECUs")
    texts = [q.text for q in qs]
    kinds = {q.kind for q in qs}

    assert len(qs) >= 4
    assert "mpn_pattern" in kinds
    assert "broker_operator" in kinds
    assert "step_back" in kinds
    assert kinds & {"hyde", "query2doc"}

    # device-class abstraction
    assert any("operational amplifier" in t.lower() for t in texts)
    # one variant carries an MPN-like token
    mpn_q = next(q for q in qs if q.kind == "mpn_pattern")
    assert _MPN_RE.search(mpn_q.text)
    # one broker-operator variant
    assert any(any(b in t.lower() for b in _BROKERS) for t in texts)
    # obsolescence vocab present in >= 2 variants
    hits = sum(1 for t in texts if any(v in t.lower() for v in OBSOLESCENCE_VOCAB))
    assert hits >= 2


def test_explicit_mpn_is_extracted():
    qs = expand_query("replacement for obsolete Hirose DF13-6P-1.25DSA connector Singapore")
    mpn_q = next(q for q in qs if q.kind == "mpn_pattern")
    assert "DF13" in mpn_q.text.upper()


def test_obsolescence_vocab_includes_core_terms():
    for term in ("obsolete", "eol", "nrnd", "nla", "ltb", "superseded by",
                 "cross reference", "equivalent", "nos"):
        assert term in OBSOLESCENCE_VOCAB


def test_planner_exposes_expand_query():
    from spider_qwen.agent.planner import Planner

    qs = Planner().expand_query("obsolete connector replacement")
    assert len(qs) >= 4


def test_merge_gather_queries_interleaves_geo_and_expansion():
    query = "16-pin SMT op-amp TI used in 90s ECUs"
    geo = build_query_templates(query, region="SEA", target_country="Singapore",
                                mode="product_exact_price")
    expanded = expand_query(query, mode="product_exact_price")
    merged = merge_gather_queries(geo, expanded, max_queries=5)
    assert merged[0] == geo[0]
    assert any("nrnd" in q.lower() or "obsolete" in q.lower() for q in merged)
    mpn_text = next(q.text for q in expanded if q.kind == "mpn_pattern")
    assert mpn_text in merge_gather_queries(geo, expanded)
    assert not any(q.kind == "hyde" and q.text in merged for q in expanded)


def test_merge_gather_queries_budget_includes_expansion():
    query = "16-pin SMT op-amp TI used in 90s ECUs"
    geo = build_query_templates(query, region="SEA", target_country="Singapore",
                                mode="product_exact_price")
    expanded = expand_query(query, mode="product_exact_price")
    capped = merge_gather_queries(geo, expanded, max_queries=3)
    assert len(capped) == 3
    assert any("nrnd" in q.lower() or "obsolete" in q.lower() for q in capped)
