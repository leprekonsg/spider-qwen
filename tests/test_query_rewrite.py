"""T-1.2: Step-Back + HyDE + Query2Doc query expansion with obsolescence vocab.

Deterministic and offline (no API): the golden test must pass with no keys.

Mode-gating regression tests (run_9fbe2f94d8d1 post-mortem):
- service_quote_required and contact_enrichment_only must not emit electronics kinds
- electronics_substitution must still emit them (no regression)
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

# Terms that must not appear in service/contact mode expansions.
_ELECTRONICS_STRINGS = ("eol", "nrnd", "nla", "ltb", "cross reference", "equivalent",
                        "obsolete", "superseded", "broker", "rochester", "lansdale",
                        "oemsecrets", "octopart", "avnet", "datasheet", "eol devices")
_ELECTRONICS_KINDS = frozenset({"obsolescence", "mpn_pattern", "broker_operator"})


def _has_electronics_noise(texts: list[str]) -> bool:
    """True if any query text contains electronics-distributor vocabulary."""
    joined = " ".join(texts).lower()
    return any(s in joined for s in _ELECTRONICS_STRINGS)


def test_expand_returns_search_query_objects():
    qs = expand_query("office cleaning Singapore")
    assert qs and all(isinstance(q, SearchQuery) for q in qs)
    assert all(q.text and q.kind for q in qs)


def test_expand_distinct_and_length_capped():
    qs = expand_query("16-pin SMT op-amp TI used in 90s ECUs",
                      mode="electronics_substitution")
    texts = [q.text for q in qs]
    assert len(set(texts)) == len(texts)
    assert all(len(t) <= 200 for t in texts)


def test_golden_obsolete_opamp_expansion():
    """Electronics mode must still emit all kinds including mpn/broker/obsolescence."""
    qs = expand_query("16-pin SMT op-amp TI used in 90s ECUs",
                      mode="electronics_substitution")
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
    qs = expand_query("replacement for obsolete Hirose DF13-6P-1.25DSA connector Singapore",
                      mode="electronics_substitution")
    mpn_q = next(q for q in qs if q.kind == "mpn_pattern")
    assert "DF13" in mpn_q.text.upper()


def test_obsolescence_vocab_includes_core_terms():
    for term in ("obsolete", "eol", "nrnd", "nla", "ltb", "superseded by",
                 "cross reference", "equivalent", "nos"):
        assert term in OBSOLESCENCE_VOCAB


def test_planner_exposes_expand_query():
    from spider_qwen.agent.planner import Planner

    qs = Planner().expand_query("obsolete connector replacement",
                                mode="electronics_substitution")
    assert len(qs) >= 4


def test_merge_gather_queries_interleaves_geo_and_expansion():
    """With electronics mode the merged list must contain obsolescence/MPN variants."""
    query = "16-pin SMT op-amp TI used in 90s ECUs"
    geo = build_query_templates(query, region="SEA", target_country="Singapore",
                                mode="electronics_substitution")
    expanded = expand_query(query, mode="electronics_substitution")
    merged = merge_gather_queries(geo, expanded, max_queries=5)
    assert merged[0] == geo[0]
    assert any("nrnd" in q.lower() or "obsolete" in q.lower() for q in merged)
    mpn_text = next(q.text for q in expanded if q.kind == "mpn_pattern")
    assert mpn_text in merge_gather_queries(geo, expanded)
    assert not any(q.kind == "hyde" and q.text in merged for q in expanded)


def test_merge_gather_queries_budget_includes_expansion():
    """With electronics mode and a tight budget, electronics variants still appear."""
    query = "16-pin SMT op-amp TI used in 90s ECUs"
    geo = build_query_templates(query, region="SEA", target_country="Singapore",
                                mode="electronics_substitution")
    expanded = expand_query(query, mode="electronics_substitution")
    capped = merge_gather_queries(geo, expanded, max_queries=3)
    assert len(capped) == 3
    assert any("nrnd" in q.lower() or "obsolete" in q.lower() for q in capped)


# ---------------------------------------------------------------------------
# Regression tests: run_9fbe2f94d8d1 post-mortem (mode-gating)
# ---------------------------------------------------------------------------

def test_service_mode_emits_no_electronics_kinds():
    """service_quote_required must not emit obsolescence/mpn_pattern/broker_operator."""
    qs = expand_query("pest control services for office building Singapore",
                      mode="service_quote_required")
    kinds = {q.kind for q in qs}
    assert not (kinds & _ELECTRONICS_KINDS), (
        f"electronics kinds leaked into service_quote_required: {kinds & _ELECTRONICS_KINDS}"
    )


def test_service_mode_emits_no_electronics_text():
    """No query text from service_quote_required should contain electronics vocabulary."""
    qs = expand_query("pest control services for office building Singapore",
                      mode="service_quote_required")
    texts = [q.text for q in qs]
    assert not _has_electronics_noise(texts), (
        f"electronics vocabulary leaked into service queries: {texts}"
    )


def test_service_mode_still_has_baseline_kinds():
    """service_quote_required must still emit original, step_back, and query2doc."""
    qs = expand_query("pest control services for office building Singapore",
                      mode="service_quote_required")
    kinds = {q.kind for q in qs}
    assert "original" in kinds
    assert "step_back" in kinds
    assert kinds & {"query2doc", "hyde"}


def test_contact_enrichment_emits_no_electronics_kinds():
    """contact_enrichment_only must not emit obsolescence/mpn_pattern/broker_operator."""
    qs = expand_query("find email for pest control company Singapore",
                      mode="contact_enrichment_only")
    kinds = {q.kind for q in qs}
    assert not (kinds & _ELECTRONICS_KINDS), (
        f"electronics kinds leaked into contact_enrichment_only: {kinds & _ELECTRONICS_KINDS}"
    )


def test_contact_enrichment_emits_no_electronics_text():
    """No query text from contact_enrichment_only should contain electronics vocabulary."""
    qs = expand_query("find email for pest control company Singapore",
                      mode="contact_enrichment_only")
    texts = [q.text for q in qs]
    assert not _has_electronics_noise(texts), (
        f"electronics vocabulary leaked into contact queries: {texts}"
    )


def test_electronics_substitution_still_emits_all_kinds():
    """electronics_substitution must still emit all three electronics kinds (no regression)."""
    qs = expand_query("replacement for obsolete STM32F103 microcontroller",
                      mode="electronics_substitution")
    kinds = {q.kind for q in qs}
    assert "obsolescence" in kinds
    assert "mpn_pattern" in kinds
    assert "broker_operator" in kinds


def test_electronics_substitution_obsolescence_text_present():
    """The obsolescence query for electronics mode must contain EOL vocabulary."""
    qs = expand_query("replacement for obsolete STM32F103 microcontroller",
                      mode="electronics_substitution")
    obs = next(q for q in qs if q.kind == "obsolescence")
    low = obs.text.lower()
    assert any(v in low for v in ("eol", "nrnd", "ltb")), obs.text


def test_no_mode_does_not_emit_electronics_kinds():
    """Callers that omit mode (None) must not get electronics kinds."""
    qs = expand_query("pest control services for office building Singapore")
    kinds = {q.kind for q in qs}
    assert not (kinds & _ELECTRONICS_KINDS), (
        f"electronics kinds emitted with mode=None: {kinds & _ELECTRONICS_KINDS}"
    )


def test_product_mode_emits_no_electronics_kinds():
    """product_exact_price is for chairs/keyboards (per docs) — no electronics kinds."""
    qs = expand_query("ergonomic office chairs Singapore price", mode="product_exact_price")
    kinds = {q.kind for q in qs}
    assert not (kinds & _ELECTRONICS_KINDS), (
        f"electronics kinds leaked into product_exact_price: {kinds & _ELECTRONICS_KINDS}"
    )


def test_service_merge_gather_no_electronics_noise():
    """merge_gather_queries for service mode must not include electronics strings."""
    query = "pest control services for office building Singapore"
    geo = build_query_templates(query, region="SEA", target_country="Singapore",
                                mode="service_quote_required")
    expanded = expand_query(query, mode="service_quote_required")
    merged = merge_gather_queries(geo, expanded)
    assert not _has_electronics_noise(merged), (
        f"electronics vocabulary in merged service queries: "
        f"{[q for q in merged if _has_electronics_noise([q])]}"
    )
