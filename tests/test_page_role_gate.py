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


def test_vendor_mention_lead_drained_into_candidate(monkeypatch):
    """A vendor named on a gated directory page must end up fetched and
    extracted via its entity follow-up query, not stranded in the queue."""
    monkeypatch.setenv("SPIDER_QWEN_FRONTIER_ENABLED", "1")
    directory_url = "https://top10cleaners.example/best-office-cleaners"

    class _MentionAwareSearch(_FixedSearch):
        async def search(self, query, location=None, language="en", limit=10):
            if "Sparkle Facilities" in query:
                return SearchResultSet(
                    query=query,
                    results=[SearchResult(
                        rank=0, source_tool="mock", url=VENDOR_URL,
                        title="Sparkle Facilities Pte Ltd",
                        snippet="Office cleaning Singapore. Request quotation.")],
                    total_results=1, provider="mock",
                )
            return await super().search(query, location, language, limit)

    search = _MentionAwareSearch([{
        "url": directory_url,
        "title": "Top 10 Office Cleaners in Singapore",
        "snippet": "The best office cleaning companies in Singapore.",
    }])
    fetch = MockFetchProvider(fixtures={
        directory_url: {"title": "Top 10 Office Cleaners in Singapore",
                        "text": "Our favourite is Sparkle Facilities Pte Ltd.",
                        "links": []},
        VENDOR_URL: {"title": "Sparkle Facilities Pte Ltd",
                     "text": VENDOR_TEXT, "links": []},
    })

    def build(text, url):
        if url == directory_url:
            return QwenPageExtraction(
                page_role="directory",
                vendor_mentions=[QwenVendorMention(name="Sparkle Facilities Pte Ltd")],
            )
        return QwenPageExtraction(
            page_role="vendor_offering",
            vendor=QwenVendorExtraction(name="Sparkle Facilities Pte Ltd"),
        )

    controller = Controller(
        offline=True, state_dir=None, persist=False,
        search_provider=search, fetch_provider=fetch,
        qwen_json_extractor=_ScriptedQwen(build),
    )
    result = asyncio.run(controller.run(QUERY, mode="service_quote_required"))
    assert result.metrics["frontier"]["entity_url_leads_fetched"] >= 1
    names = {c["vendor_name"] for c in result.validated_candidates}
    assert "Sparkle Facilities Pte Ltd" in names


# --- deterministic marketplace listing gate ----------------------------------


def test_marketplace_listing_urls_detected():
    from spider_qwen.agent.controller import _marketplace_listing_page

    assert _marketplace_listing_page("https://www.lazada.sg/tag/office-chair/")
    assert _marketplace_listing_page("https://shopee.sg/search?q=office+chair")
    assert _marketplace_listing_page("https://www.lazada.com.my/catalog/?q=chair")
    # Product detail pages and non-marketplace hosts are not listings.
    assert not _marketplace_listing_page("https://www.lazada.sg/products/ergo-chair-i123.html")
    assert not _marketplace_listing_page("https://sparkle.sg/tag/office-cleaning")
    assert not _marketplace_listing_page(None)


def test_marketplace_tag_page_never_becomes_candidate():
    tag_url = "https://www.lazada.sg/tag/office-chair/"
    search = _FixedSearch([{
        "url": tag_url,
        "title": "Office Chair - Buy Office Chairs Online | Lazada SG",
        "snippet": "Shop office chairs. Ergonomic office chair deals.",
    }])
    fetch = MockFetchProvider(fixtures={
        tag_url: {"title": "Office Chair | Lazada SG",
                  "text": "Ergonomic Office Chair $89.00. Mesh Chair $120.00. Add to cart.",
                  "links": []},
    })
    controller = Controller(offline=True, state_dir=None, persist=False,
                            search_provider=search, fetch_provider=fetch)
    result = asyncio.run(controller.run("ergonomic office chair Singapore",
                                        mode="product_exact_price"))
    assert result.validated_candidates == []


# --- Qwen pricing subject gate ------------------------------------------------

CHAIR_QUERY = "ergonomic office chair Singapore"
CHAIR_URL = "https://chairshop.sg/ergonomic-office-chair"
CHAIR_TEXT = (
    "ErgoPro Ergonomic Office Chair S$459.00. "
    "Memory Foam Headrest S$49.90. Add to cart."
)


def _run_product(qwen_build):
    search = _FixedSearch([{
        "url": CHAIR_URL,
        "title": "ErgoPro Ergonomic Office Chair | ChairShop",
        "snippet": "Ergonomic office chair Singapore.",
    }])
    fetch = MockFetchProvider(fixtures={
        CHAIR_URL: {"title": "ErgoPro Ergonomic Office Chair | ChairShop",
                    "text": CHAIR_TEXT, "links": []},
    })
    controller = Controller(
        offline=True, state_dir=None, persist=False,
        search_provider=search, fetch_provider=fetch,
        qwen_json_extractor=_ScriptedQwen(qwen_build),
    )
    return asyncio.run(controller.run(CHAIR_QUERY, mode="product_exact_price"))


def test_accessory_price_with_unrelated_subject_is_dropped():
    from spider_qwen.modes.contracts import PricingStatus
    from spider_qwen.tools.qwen_json_extractor import QwenPricingExtraction

    result = _run_product(lambda text, url: QwenPageExtraction(
        page_role="vendor_offering",
        pricing=QwenPricingExtraction(
            status=PricingStatus.EXACT_PRICE, price=49.9, currency="SGD",
            subject="Memory Foam Headrest", matched_text="S$49.90"),
    ))
    assert all(c.get("price") != 49.9 for c in result.validated_candidates)


def test_price_with_query_matching_subject_is_kept():
    from spider_qwen.modes.contracts import PricingStatus
    from spider_qwen.tools.qwen_json_extractor import QwenPricingExtraction

    result = _run_product(lambda text, url: QwenPageExtraction(
        page_role="vendor_offering",
        pricing=QwenPricingExtraction(
            status=PricingStatus.EXACT_PRICE, price=459.0, currency="SGD",
            subject="ErgoPro Ergonomic Office Chair", matched_text="S$459.00"),
    ))
    assert any(c.get("price") == 459.0 for c in result.validated_candidates)
