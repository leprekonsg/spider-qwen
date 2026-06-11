"""Lead frontier: deterministic pre-fetch scoring and the priority queue.

The fetch budget must be spent on the highest-value leads (reliability prior,
query-term overlap, geo TLD, contact-path bonus, depth penalty), not in SERP
discovery order. Ties keep discovery order so the pre-frontier behavior is
preserved exactly when scores cannot distinguish leads.
"""

from __future__ import annotations

from spider_qwen.agent.frontier import (
    DIRECTORY_MIN_DOMAINS,
    Frontier,
    Lead,
    apply_scorer_deltas,
    entity_query_lead,
    leads_from_search_results,
    link_leads_from_page,
    rank_leads,
    score_url_lead,
)
from spider_qwen.tools.provider_types import FetchResult, SearchResult


QUERY = "office cleaning Singapore"


def test_reliability_tier_orders_scores():
    distributor = score_url_lead("https://digikey.com/parts", query=QUERY)
    marketplace = score_url_lead("https://alibaba.com/parts", query=QUERY)
    assert distributor > marketplace


def test_geo_tld_target_bonus():
    sg = score_url_lead("https://cleaners.sg/services", query=QUERY, target_cc="SG")
    com = score_url_lead("https://cleaners.com/services", query=QUERY, target_cc="SG")
    sea = score_url_lead("https://cleaners.my/services", query=QUERY, target_cc="SG")
    assert sg > sea > com


def test_term_overlap_raises_score():
    on_topic = score_url_lead(
        "https://vendor.example/x", query=QUERY,
        snippet="Office cleaning services in Singapore, request a quotation.",
    )
    off_topic = score_url_lead(
        "https://vendor.example/x", query=QUERY, snippet="Unrelated blog post about weather.",
    )
    assert on_topic > off_topic


def test_depth_penalty_and_contact_bonus():
    base = score_url_lead("https://vendor.example/services", query=QUERY)
    deep = score_url_lead("https://vendor.example/services", query=QUERY, depth=1)
    contact = score_url_lead("https://vendor.example/contact", query=QUERY)
    assert deep < base
    assert contact > base


def test_rank_leads_is_stable_on_ties():
    leads = [Lead(kind="url", value=f"https://example-vendor-{i}.sg/x", score=0.5) for i in range(4)]
    assert [l.value for l in rank_leads(leads)] == [l.value for l in leads]


def test_leads_from_search_results_scores_snippets():
    results = [
        SearchResult(url="https://alibaba.com/listing", title="Listing", snippet="cheap deals"),
        SearchResult(url="https://cleanco.sg/office-cleaning", title="CleanCo Singapore",
                     snippet="Office cleaning Singapore, request a quotation."),
    ]
    ranked = rank_leads(leads_from_search_results(results, query=QUERY, target_cc="SG"))
    assert ranked[0].value == "https://cleanco.sg/office-cleaning"


def test_frontier_dedupes_canonical_urls_and_floors():
    f = Frontier(floor=0.2)
    assert f.add(Lead(kind="url", value="https://www.vendor.sg/contact/", score=0.5))
    assert not f.add(Lead(kind="url", value="https://vendor.sg/contact", score=0.9))
    assert not f.add(Lead(kind="url", value="https://weak.example/x", score=0.1))
    assert not f.add(Lead(kind="url", value="https://deep.example/x", score=0.5, depth=2))
    assert f.pending() == 1
    assert f.stats["dropped_duplicate"] == 1
    assert f.stats["dropped_floor"] == 1
    assert f.stats["dropped_depth"] == 1


def test_frontier_pop_returns_best_first_and_never_returns_seen():
    f = Frontier()
    f.add(Lead(kind="url", value="https://low.example/x", score=0.3))
    f.add(Lead(kind="url", value="https://high.example/x", score=0.9))
    f.add(Lead(kind="query", value="follow up query", score=0.5))
    popped = f.pop("url", 1)
    assert [l.value for l in popped] == ["https://high.example/x"]
    # A popped lead can never re-enter (it was fetched).
    assert not f.add(Lead(kind="url", value="https://high.example/x", score=0.9))
    assert f.pending("url") == 1
    assert f.pending("query") == 1


def test_frontier_mark_seen_blocks_already_fetched_urls():
    f = Frontier()
    f.mark_seen(Lead(kind="url", value="https://vendor.sg/page"))
    assert not f.add(Lead(kind="url", value="https://www.vendor.sg/page/", score=0.8))


def test_apply_scorer_deltas_clamps():
    leads = [
        Lead(kind="url", value="https://a.example/x", score=0.5),
        Lead(kind="url", value="https://b.example/x", score=0.5),
    ]
    moved = apply_scorer_deltas(leads, {"https://a.example/x": 0.9, "https://b.example/x": -0.9})
    assert moved == 2
    assert leads[0].score == 0.7  # clamped to +0.2, not +0.9
    assert leads[1].score == 0.3


def test_link_leads_same_domain_contact_only():
    page = FetchResult(
        url="https://cleanco.sg/", text="x",
        links=[
            "https://cleanco.sg/contact",
            "https://cleanco.sg/blog/post-1",
            "https://other.example/somewhere",
            "mailto:sales@cleanco.sg",
        ],
    )
    leads = link_leads_from_page(page, query=QUERY)
    assert [l.value for l in leads] == ["https://cleanco.sg/contact"]
    assert leads[0].provenance == "contact_link"
    assert leads[0].depth == 1
    assert leads[0].parent_url == "https://cleanco.sg/"


def test_link_leads_directory_pages_yield_cross_domain_vendors():
    vendors = [f"https://vendor-{i}.sg/profile" for i in range(DIRECTORY_MIN_DOMAINS)]
    page = FetchResult(url="https://directory.example/cleaning", text="x", links=vendors)
    leads = link_leads_from_page(page, query=QUERY)
    assert {l.value for l in leads} == set(vendors)
    assert all(l.provenance == "directory_link" for l in leads)


def test_link_leads_few_cross_domain_links_are_not_a_directory():
    page = FetchResult(
        url="https://cleanco.sg/", text="x",
        links=["https://facebook.com/cleanco", "https://instagram.com/cleanco"],
    )
    assert link_leads_from_page(page, query=QUERY) == []


def test_entity_query_lead_shape():
    lead = entity_query_lead("CleanCo Pte Ltd", "Singapore")
    assert lead.kind == "query"
    assert "CleanCo Pte Ltd" in lead.value
    assert lead.provenance == "entity_query"
    assert lead.depth == 1
