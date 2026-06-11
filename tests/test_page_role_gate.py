"""Qwen page-role gate, grounded vendor-name override, vendor-mention leads.

Qwen proposes (page_role, vendor.name, vendor_mentions); deterministic code
verifies: suppression is tighten-only, names must be grounded in page text,
mentions become frontier query leads instead of candidates.
"""

from __future__ import annotations

import asyncio

from spider_qwen.agent.controller import Controller
from spider_qwen.tools.fetch_service import MockFetchProvider
from spider_qwen.tools.provider_types import SearchResult, SearchResultSet
from spider_qwen.tools.qwen_json_extractor import (
    QwenPageExtraction,
    QwenVendorExtraction,
    QwenVendorMention,
)

QUERY = "office cleaning Singapore"
VENDOR_URL = "https://sparkle.sg/office-cleaning-singapore"
VENDOR_TEXT = (
    "Sparkle Facilities Pte Ltd provides office cleaning across Singapore. "
    "Request a quotation at sales@sparkle.sg."
)


class _FixedSearch:
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


class _ScriptedQwen:
    def __init__(self, build) -> None:
        self._build = build

    def extract(self, *, text: str, page_url: str, query: str) -> QwenPageExtraction:
        return self._build(text, page_url)


def _vendor_providers():
    search = _FixedSearch([{
        "url": VENDOR_URL,
        "title": "Best Office Cleaning in Singapore | Sparkle",
        "snippet": "Office cleaning Singapore. Request quotation.",
    }])
    fetch = MockFetchProvider(fixtures={
        VENDOR_URL: {"title": "Best Office Cleaning in Singapore | Sparkle",
                     "text": VENDOR_TEXT, "links": []},
    })
    return search, fetch


def _run(qwen_build, monkeypatch=None, frontier=False):
    if frontier:
        monkeypatch.setenv("SPIDER_QWEN_FRONTIER_ENABLED", "1")
    search, fetch = _vendor_providers()
    controller = Controller(
        offline=True, state_dir=None, persist=False,
        search_provider=search, fetch_provider=fetch,
        qwen_json_extractor=_ScriptedQwen(qwen_build),
    )
    return asyncio.run(controller.run(QUERY, mode="service_quote_required"))


def test_buyer_rfq_page_never_becomes_candidate():
    result = _run(lambda text, url: QwenPageExtraction(page_role="buyer_rfq"))
    assert result.validated_candidates == []


def test_grounded_vendor_name_overrides_title_heuristic():
    result = _run(lambda text, url: QwenPageExtraction(
        page_role="vendor_offering",
        vendor=QwenVendorExtraction(name="Sparkle Facilities Pte Ltd"),
    ))
    names = {c["vendor_name"] for c in result.validated_candidates}
    assert "Sparkle Facilities Pte Ltd" in names


def test_ungrounded_vendor_name_is_ignored():
    result = _run(lambda text, url: QwenPageExtraction(
        page_role="vendor_offering",
        vendor=QwenVendorExtraction(name="Phantom Industries XYZ"),
    ))
    assert result.validated_candidates
    names = {c["vendor_name"] for c in result.validated_candidates}
    assert "Phantom Industries XYZ" not in names
    # Title heuristic survives as the fallback.
    assert any("Sparkle" in n for n in names)


def test_vendor_mentions_become_entity_query_leads(monkeypatch):
    def build(text, url):
        if url == VENDOR_URL:
            return QwenPageExtraction(
                page_role="directory",
                vendor_mentions=[
                    QwenVendorMention(name="Sparkle Facilities Pte Ltd"),  # grounded
                    QwenVendorMention(name="Phantom Industries XYZ"),      # not on page
                ],
            )
        return QwenPageExtraction(page_role="vendor_offering")

    result = _run(build, monkeypatch, frontier=True)
    assert result.metrics["frontier"]["entity_query_leads_inserted"] >= 1
    # The directory page itself was suppressed as a candidate.
    assert all(c["vendor_name"] != "Best Office Cleaning in Singapore | Sparkle"
               for c in result.validated_candidates)


def test_qwen_failure_keeps_deterministic_candidate():
    def boom(text, url):
        raise RuntimeError("model down")

    result = _run(boom)
    assert result.validated_candidates  # degraded to deterministic extraction
