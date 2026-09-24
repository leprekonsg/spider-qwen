"""Live-web fetch-outcome taxonomy: bot walls, JS shells, geo-blocks, empty and
thin pages are distinct signals; the run metrics must report the histogram so a
starved live run says WHY it starved instead of just "insufficient evidence".
"""

from __future__ import annotations

import asyncio

from spider_qwen.agent.controller import Controller
from spider_qwen.tools.fetch_failures import (
    ERROR_DEAD_LINK,
    ERROR_TRANSPORT,
    PAGE_BOT_WALL,
    PAGE_EMPTY,
    PAGE_GEO_BLOCK,
    PAGE_JS_SHELL,
    PAGE_OK,
    PAGE_THIN,
    classify_error,
    classify_page,
)
from spider_qwen.tools.fetch_service import MockFetchProvider
from spider_qwen.tools.provider_types import SearchResult, SearchResultSet

OK_TEXT = (
    "We provide office cleaning services across Singapore for commercial "
    "buildings. Request a quotation by emailing sales@vendor.sg or call "
    "+65 6123 4567. Our teams cover daily, weekly and post-renovation cleans."
)


def test_classify_page_bot_wall():
    assert classify_page("Attention Required! Please verify you are a human.") == PAGE_BOT_WALL
    assert classify_page("Checking your browser. Cloudflare. Just a moment...") == PAGE_BOT_WALL


def test_classify_page_js_shell():
    assert classify_page("Please enable JavaScript to view this site.") == PAGE_JS_SHELL


def test_classify_page_geo_block():
    assert classify_page("This service is not available in your country.") == PAGE_GEO_BLOCK


def test_classify_page_empty_and_thin():
    assert classify_page("") == PAGE_EMPTY
    assert classify_page("   \n  ") == PAGE_EMPTY
    assert classify_page("Welcome.") == PAGE_THIN


def test_classify_page_ok_and_long_page_with_incidental_keywords():
    assert classify_page(OK_TEXT) == PAGE_OK
    # A long legitimate page mentioning "captcha" must not be misclassified:
    # interstitial walls are short.
    long_page = ("We sell captcha-solving research papers and javascript "
                 "training courses. " * 60)
    assert classify_page(long_page) == PAGE_OK


def test_classify_error():
    assert classify_error("HTTP 404", 404) == ERROR_DEAD_LINK
    assert classify_error("HTTP 410", 410) == ERROR_DEAD_LINK
    assert classify_error("HTTP 403", 403) == PAGE_BOT_WALL
    assert classify_error("HTTP 429", 429) == PAGE_BOT_WALL
    assert classify_error("connection timeout") == ERROR_TRANSPORT
    assert classify_error("HTTP 404") == ERROR_DEAD_LINK  # status parsed from text


class _FixedSearch:
    provider_name = "mock"
    search_source_tool = "mock"
    rate_limited = False

    def __init__(self, urls: list[str]) -> None:
        self._urls = urls

    async def search(self, query, location=None, language="en", limit=10):
        return SearchResultSet(
            query=query,
            results=[SearchResult(url=u, title=u, snippet="office cleaning Singapore",
                                  rank=i, source_tool="mock")
                     for i, u in enumerate(self._urls)],
            total_results=len(self._urls),
            provider="mock",
        )


def test_run_metrics_report_fetch_outcome_histogram():
    fixtures = {
        "https://ok-vendor.sg/office-cleaning": {"text": OK_TEXT},
        "https://walled.sg/office-cleaning": {
            "text": "Attention Required! Please verify you are a human. captcha"},
        "https://shell.sg/office-cleaning": {
            "text": "Please enable JavaScript to continue."},
        "https://geo.sg/office-cleaning": {
            "text": "This page is not available in your region."},
        "https://blank.sg/office-cleaning": {"text": ""},
        "https://dead.sg/office-cleaning": {"status": 404},
    }
    controller = Controller(
        offline=True,
        search_provider=_FixedSearch(list(fixtures)),
        fetch_provider=MockFetchProvider(fixtures=fixtures),
    )
    result = asyncio.run(controller.run("office cleaning Singapore"))
    outcomes = result.metrics["fetch_outcomes"]
    assert outcomes[PAGE_OK] >= 1
    assert outcomes[PAGE_BOT_WALL] >= 1
    assert outcomes[PAGE_JS_SHELL] >= 1
    assert outcomes[PAGE_GEO_BLOCK] >= 1
    assert outcomes[PAGE_EMPTY] >= 1
    assert outcomes[ERROR_DEAD_LINK] >= 1


def test_default_offline_run_reports_ok_pages():
    result = asyncio.run(Controller(offline=True).run("office cleaning Singapore"))
    outcomes = result.metrics["fetch_outcomes"]
    # Mock pages are real extractable text: the histogram must say so, not
    # count phantom failures.
    assert outcomes.get(PAGE_OK, 0) >= 1
    assert PAGE_BOT_WALL not in outcomes


def test_fetch_class_recorded_on_evidence_metadata():
    from spider_qwen.evidence.ledger import EvidenceLedger
    from spider_qwen.tools.fetch_service import FetchService

    url = "https://walled.sg/office-cleaning"
    ledger = EvidenceLedger("run_test_fetch_class")
    service = FetchService(
        MockFetchProvider(fixtures={url: {
            "text": "Access denied. Please verify you are a human."}}),
        ledger,
    )
    asyncio.run(service.fetch([url]))
    (item,) = ledger.items()
    assert item.metadata["fetch_class"] == PAGE_BOT_WALL
    assert service.fetch_outcomes == {PAGE_BOT_WALL: 1}


# --- taxonomy-triggered fallback retry ---------------------------------------


class _RecordingFallback:
    provider_name = "qwen_web_extractor"
    fetch_source_tool = "qwen_web_extractor"
    rate_limited = True

    def __init__(self, text: str = OK_TEXT) -> None:
        self.calls: list[list[str]] = []
        self._text = text

    async def fetch(self, urls, output_format="markdown", include_links=True):
        from spider_qwen.tools.provider_types import FetchResult, FetchResultSet

        self.calls.append(list(urls))
        return FetchResultSet(
            results=[FetchResult(url=u, final_url=u, title="Recovered",
                                 text=self._text, source_tool="qwen_web_extractor")
                     for u in urls],
            provider="qwen_web_extractor",
        )


def _fallback_service(fixtures, fallback):
    from spider_qwen.evidence.ledger import EvidenceLedger
    from spider_qwen.tools.fetch_service import FetchService

    ledger = EvidenceLedger("run_test_fallback")
    return FetchService(MockFetchProvider(fixtures=fixtures), ledger,
                        fallback=fallback), ledger


def test_transport_error_retried_through_fallback():
    url = "https://flaky.sg/office-cleaning"
    fallback = _RecordingFallback()
    service, ledger = _fallback_service({url: {"status": 500}}, fallback)
    rs = asyncio.run(service.fetch([url]))
    assert fallback.calls == [[url]]
    assert [p.url for p in rs.results] == [url]
    assert service.fallback_recovered == 1
    assert rs.errors[0]["recovered_via"] == "qwen_web_extractor"
    (item,) = ledger.items()
    assert item.source_tool == "qwen_web_extractor"
    assert item.metadata["fetch_fallback"]["provider"] == "qwen_web_extractor"
    # Both attempts appear in the histogram: the failure and the recovery.
    assert service.fetch_outcomes == {ERROR_TRANSPORT: 1, PAGE_OK: 1}


def test_js_shell_replaced_by_fallback_page():
    url = "https://shell.sg/office-cleaning"
    fallback = _RecordingFallback()
    service, ledger = _fallback_service(
        {url: {"text": "Please enable JavaScript to continue."}}, fallback)
    rs = asyncio.run(service.fetch([url]))
    assert fallback.calls == [[url]]
    (item,) = ledger.items()  # the shell never reached the ledger
    assert item.text == OK_TEXT
    assert service.fetch_outcomes == {PAGE_JS_SHELL: 1, PAGE_OK: 1}


def test_dead_link_and_bot_wall_are_not_retried():
    fallback = _RecordingFallback()
    service, _ = _fallback_service({
        "https://dead.sg/x": {"status": 404},
        "https://walled.sg/x": {"text": "Access denied. verify you are a human."},
    }, fallback)
    asyncio.run(service.fetch(["https://dead.sg/x", "https://walled.sg/x"]))
    assert fallback.calls == []


def test_fallback_failure_leaves_original_outcome():
    class _Boom:
        provider_name = "qwen_web_extractor"
        fetch_source_tool = "qwen_web_extractor"
        rate_limited = True

        async def fetch(self, urls, output_format="markdown", include_links=True):
            raise RuntimeError("model down")

    url = "https://flaky.sg/office-cleaning"
    service, _ = _fallback_service({url: {"status": 500}}, _Boom())
    rs = asyncio.run(service.fetch([url]))
    assert rs.results == []
    assert service.fallback_recovered == 0
    assert service.fetch_outcomes == {ERROR_TRANSPORT: 1}


def test_fallback_retry_spends_fetch_budget():
    from spider_qwen.agent.budget import Budget, BudgetTracker

    url = "https://flaky.sg/office-cleaning"
    fallback = _RecordingFallback()
    service, _ = _fallback_service({url: {"status": 500}}, fallback)
    service.tracker = BudgetTracker(Budget(mode="service_quote_required", max_fetch_urls=2))
    asyncio.run(service.fetch([url]))
    assert fallback.calls == [[url]]
    assert service.tracker.fetch_urls == 2  # the first attempt and the retry


def test_fallback_retry_skipped_when_fetch_budget_is_spent():
    from spider_qwen.agent.budget import Budget, BudgetTracker

    url = "https://flaky.sg/office-cleaning"
    fallback = _RecordingFallback()
    service, _ = _fallback_service({url: {"status": 500}}, fallback)
    service.tracker = BudgetTracker(Budget(mode="service_quote_required", max_fetch_urls=1))
    asyncio.run(service.fetch([url]))
    assert fallback.calls == []
    assert service.tracker.stop_reason is None
