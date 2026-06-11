"""Frontier gather loop (SPIDER_QWEN_FRONTIER_ENABLED): drain best-first,
1-hop link insertion, entity-conditioned follow-up queries, Qwen rescore seam.

The flag off must leave the linear gather path untouched; the flag on must
stay inside the same budget caps it replaces.
"""

from __future__ import annotations

import asyncio

import pytest

from spider_qwen.agent.controller import Controller
from spider_qwen.agent.qwen_frontier_scorer import parse_deltas
from spider_qwen.tools.fetch_service import MockFetchProvider
from spider_qwen.tools.provider_types import SearchResult, SearchResultSet

QUERY = "office cleaning Singapore"


class _FixedSearch:
    """Returns the same fixed results for every query (mock synthesis would
    otherwise invent vendors and drown the fixture under test)."""

    provider_name = "mock"
    search_source_tool = "mock"
    rate_limited = False

    def __init__(self, results: list[dict]) -> None:
        self._results = results

    async def search(self, query, location=None, language="en", limit=10):
        return SearchResultSet(
            query=query,
            results=[SearchResult(rank=i, source_tool="mock", **r)
                     for i, r in enumerate(self._results)],
            total_results=len(self._results),
            provider="mock",
        )


def _run(controller: Controller):
    return asyncio.run(controller.run(QUERY))


def test_flag_off_reports_frontier_disabled():
    result = _run(Controller(offline=True))
    assert result.metrics["frontier"] == {"enabled": False}


def test_frontier_run_stays_within_budget(monkeypatch):
    monkeypatch.setenv("SPIDER_QWEN_FRONTIER_ENABLED", "1")
    result = _run(Controller(offline=True))
    frontier = result.metrics["frontier"]
    assert frontier["enabled"] is True
    assert frontier["rounds"] >= 1
    budget = result.budget
    assert budget["search_calls"] <= budget["max_search_calls"]
    assert budget["fetch_urls"] <= budget["max_fetch_urls"]
    assert budget["candidates_extracted"] <= budget["max_candidates_to_extract"]


DIRECTORY_URL = "https://sg-cleaning-directory.example/cleaning-vendors"
VENDOR_URLS = [f"https://vendor-{i}.sg/office-cleaning-singapore" for i in range(1, 7)]


def _directory_providers():
    search = _FixedSearch([{
        "url": DIRECTORY_URL,
        "title": "Singapore Cleaning Directory",
        "snippet": "Directory of office cleaning vendors in Singapore. Request quotation.",
    }])
    fetch = MockFetchProvider(fixtures={
        DIRECTORY_URL: {
            "title": "Singapore Cleaning Directory",
            "text": "Directory of office cleaning vendors in Singapore.",
            "links": VENDOR_URLS,
        },
    })
    return search, fetch


def test_directory_page_yields_multiple_vendor_candidates(monkeypatch):
    monkeypatch.setenv("SPIDER_QWEN_FRONTIER_ENABLED", "1")
    search, fetch = _directory_providers()
    result = _run(Controller(offline=True, search_provider=search, fetch_provider=fetch))
    frontier = result.metrics["frontier"]
    assert frontier["link_leads_inserted"] >= len(VENDOR_URLS)
    # The directory page is one fetch; the frontier converts it into many leads.
    assert result.metrics["candidates_considered"] >= 5
    assert len(result.validated_candidates) >= 2


def test_directory_page_without_frontier_yields_one_candidate():
    search, fetch = _directory_providers()
    result = _run(Controller(offline=True, search_provider=search, fetch_provider=fetch))
    # Linear path: extraction only ever sees the SERP URL, so 50 listed
    # vendors collapse into a single weak candidate. This is the contrast the
    # frontier exists to fix.
    assert result.metrics["candidates_considered"] <= 2
    assert result.metrics["frontier"] == {"enabled": False}


def test_contact_links_enter_frontier_on_default_mocks(monkeypatch):
    monkeypatch.setenv("SPIDER_QWEN_FRONTIER_ENABLED", "1")
    # Default mock pages link to /contact and /request-a-quote on their own
    # domain; those must enter the queue as depth-1 contact_link leads.
    result = _run(Controller(offline=True))
    assert result.metrics["frontier"]["link_leads_inserted"] >= 1


def test_ungrounded_vendor_earns_entity_query_lead(monkeypatch):
    monkeypatch.setenv("SPIDER_QWEN_FRONTIER_ENABLED", "1")
    url = "https://ghostvendor.sg/office-cleaning-singapore"
    search = _FixedSearch([{
        "url": url,
        "title": "GhostVendor Pte Ltd",
        "snippet": "Office cleaning Singapore.",
    }])
    fetch = MockFetchProvider(fixtures={
        url: {
            "title": "GhostVendor Pte Ltd",
            # No email/phone: a vendor we found but cannot ground a quote
            # channel for, which is exactly what earns a follow-up query.
            "text": "We provide office cleaning services across Singapore.",
            "links": [],
        },
    })
    result = _run(Controller(offline=True, search_provider=search, fetch_provider=fetch))
    assert result.metrics["frontier"]["entity_query_leads_inserted"] >= 1


class _QueryAwareSearch:
    """Routes entity follow-up queries (quoted vendor name) to their own results."""

    provider_name = "mock"
    search_source_tool = "mock"
    rate_limited = False

    def __init__(self, default: list[dict], entity_marker: str, entity: list[dict]) -> None:
        self._default = default
        self._marker = entity_marker
        self._entity = entity

    async def search(self, query, location=None, language="en", limit=10):
        rows = self._entity if self._marker in query else self._default
        return SearchResultSet(
            query=query,
            results=[SearchResult(rank=i, source_tool="mock", **r)
                     for i, r in enumerate(rows)],
            total_results=len(rows),
            provider="mock",
        )


def test_entity_fetch_reserve_drains_follow_up_leads(monkeypatch):
    """Without held-back budget, round 1 SERP leads consume every fetch slot and
    entity follow-up queries die in the queue (live finding: recall capped with
    named vendors stranded). The reserve must leave room to fetch them."""
    monkeypatch.setenv("SPIDER_QWEN_FRONTIER_ENABLED", "1")
    ghost = "https://ghostvendor.sg/office-cleaning-singapore"
    contact = "https://ghostvendor.sg/contact-us"
    fillers = [f"https://filler-{i}.sg/office-cleaning-singapore" for i in range(12)]
    search = _QueryAwareSearch(
        default=[{"url": ghost, "title": "GhostVendor Pte Ltd",
                  "snippet": "Office cleaning Singapore."}]
        + [{"url": u, "title": f"Cleaner {u}", "snippet": "Office cleaning Singapore."}
           for u in fillers],
        entity_marker="GhostVendor",
        entity=[{"url": contact, "title": "GhostVendor Pte Ltd | Contact",
                 "snippet": "Request a quotation from GhostVendor."}],
    )
    fixtures = {
        # A vendor without a quote channel earns the entity follow-up query.
        ghost: {"title": "GhostVendor Pte Ltd",
                "text": "We provide office cleaning services across Singapore.",
                "links": []},
        contact: {"title": "GhostVendor Pte Ltd | Contact",
                  "text": "GhostVendor Pte Ltd office cleaning. "
                          "Request a quotation at sales@ghostvendor.sg.",
                  "links": []},
    }
    for u in fillers:
        fixtures[u] = {"title": f"Cleaner {u}",
                       "text": "We provide office cleaning services across Singapore.",
                       "links": []}
    result = _run(Controller(offline=True, search_provider=search,
                             fetch_provider=MockFetchProvider(fixtures=fixtures)))
    frontier = result.metrics["frontier"]
    assert frontier["entity_query_leads_inserted"] >= 1
    assert frontier["entity_url_leads_fetched"] >= 1
    budget = result.budget
    assert budget["fetch_urls"] <= budget["max_fetch_urls"]
    assert budget["search_calls"] <= budget["max_search_calls"]


def test_qwen_scorer_seam_reorders_offline(monkeypatch):
    monkeypatch.setenv("SPIDER_QWEN_FRONTIER_ENABLED", "1")
    monkeypatch.setenv("QWEN_FRONTIER_SCORER_ENABLED", "1")
    result = _run(Controller(offline=True))
    # The offline mock scorer boosts contact/RFQ-shaped URLs; the run records
    # how many leads it actually moved (clamped, reorder-only).
    assert result.metrics["frontier"].get("qwen_scorer_moved", 0) >= 1


def test_parse_deltas_accepts_index_delta_lines_only():
    leads = [("https://a.example/x", 0.5, "serp"), ("https://b.example/x", 0.4, "serp")]
    deltas = parse_deltas("0 0.2\n1 -0.1\n9 0.3\nnot a line\n", leads)
    assert deltas == {"https://a.example/x": 0.2, "https://b.example/x": -0.1}


def test_parse_deltas_empty_response_is_no_op():
    assert parse_deltas("", [("https://a.example/x", 0.5, "serp")]) == {}
