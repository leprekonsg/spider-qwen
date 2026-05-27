from __future__ import annotations

import asyncio

from spider_qwen.agent.policy import Policy
from spider_qwen.agent.controller import Controller
from spider_qwen.tools.provider_types import SearchResult, SearchResultSet
from spider_qwen.tools.fetch_service import MockFetchProvider
from spider_qwen.tools.search_service import MockSearchProvider


def _controller() -> Controller:
    return Controller(
        search_provider=MockSearchProvider(),
        fetch_provider=MockFetchProvider(),
        state_dir=None,
        persist=False,
    )


def test_service_run_produces_rfq_and_evidence():
    result = asyncio.run(_controller().run("office cleaning Singapore", mode="auto"))
    assert result.mode == "service_quote_required"
    assert result.rfq_drafts, "expected at least one RFQ draft"
    assert result.evidence_refs, "every validated candidate must carry evidence"
    # v1 boundary: drafts only, never submitted.
    for draft in result.rfq_drafts:
        assert "draft only" in " ".join(draft["assumptions_and_limits"]).lower()


def test_product_run_has_pricing_summary():
    result = asyncio.run(_controller().run("bulk printer paper supplier Singapore price per unit",
                                           mode="product_exact_price"))
    assert result.mode == "product_exact_price"
    assert result.validated_candidates
    assert result.pricing_status_summary
    assert all(c["evidence_refs"] for c in result.validated_candidates)
    assert all(c["pricing_status"] in {"EXACT_PRICE", "PRICE_RANGE", "STARTING_FROM", "RATE_CARD_FOUND"}
               for c in result.validated_candidates)
    assert result.rfq_drafts == []  # product mode never generates RFQs


def test_contact_run_returns_contacts():
    result = asyncio.run(_controller().run("find contact email for Example Cleaning Pte Ltd",
                                           mode="contact_enrichment_only"))
    assert result.mode == "contact_enrichment_only"


def test_every_validated_candidate_has_evidence_ref():
    result = asyncio.run(_controller().run("pest control Singapore RFQ", mode="auto"))
    for cand in result.validated_candidates:
        assert cand["evidence_refs"], "candidate without evidence_ref leaked into output"


def test_stop_reason_present():
    result = asyncio.run(_controller().run("office cleaning Singapore", mode="auto"))
    assert result.stop_reason


def test_global_fallback_drops_local_search_location():
    class EmptySeaSearchProvider:
        provider_name = "empty_sea"
        search_source_tool = "mock"

        def __init__(self) -> None:
            self.calls: list[tuple[str, str | None]] = []

        async def search(self, query: str, location: str | None, language: str, limit: int):
            self.calls.append((query, location))
            if "vendor" not in query:
                return SearchResultSet(query=query, location=location, provider="mock")
            return SearchResultSet(
                query=query,
                location=location,
                provider="mock",
                results=[SearchResult(url="https://global-cleaning.example/services", title="Global Cleaning")],
                total_results=1,
            )

    provider = EmptySeaSearchProvider()
    result = asyncio.run(Controller(
        search_provider=provider,
        fetch_provider=MockFetchProvider(),
        state_dir=None,
        persist=False,
    ).run("office cleaning", mode="service_quote_required"))

    assert result.validated_candidates
    assert any("vendor" in query and location is None for query, location in provider.calls)


def test_extraction_budget_is_enforced_end_to_end():
    policy = Policy({
        "geo": {"default_region": "SEA", "boost_countries": ["Singapore"]},
        "budgets": {
            "service_quote_required": {
                "max_search_calls": 1,
                "max_fetch_urls": 5,
                "max_candidates_to_extract": 1,
                "max_validated_candidates": 5,
                "min_validated_candidates": 1,
                "evidence_completeness_threshold": 0.65,
            }
        },
        "rfq": {"minimum_checklist_completeness": 0.65, "allow_vendor_submission": False},
        "memory": {"semantic_promotion_requires_evidence": True, "allow_disputed_facts_in_rfq": False},
    })
    result = asyncio.run(Controller(
        policy=policy,
        search_provider=MockSearchProvider(),
        fetch_provider=MockFetchProvider(),
        state_dir=None,
        persist=False,
    ).run("office cleaning Singapore", mode="service_quote_required"))

    assert result.budget["candidates_extracted"] == 1
    assert result.metrics["candidates_considered"] == 1
