"""T-1.3: Search-o1 mid-reasoning condensation + CRAG corrective evaluator.

Deterministic relevance heuristic; on an "incorrect" verdict the controller
pivots to broaden/broker queries instead of answering from off-target pages.
"""

from __future__ import annotations

import asyncio

from spider_qwen.agent.controller import Controller
from spider_qwen.serendipity.corrective import (
    CorrectiveVerdict,
    corrective_queries,
    evaluate_retrieval,
)
from spider_qwen.tools.fetch_service import MockFetchProvider
from spider_qwen.tools.provider_types import (
    FetchResult,
    FetchResultSet,
    SearchResult,
    SearchResultSet,
)
from spider_qwen.tools.search_service import MockSearchProvider


class _Page:
    def __init__(self, text: str, title: str = "", url: str = "https://x.example"):
        self.text = text
        self.title = title
        self.url = url
        self.final_url = url


def test_evaluate_correct_for_relevant_pages():
    pages = [_Page("We supply office cleaning services in Singapore. Request a quotation.",
                   "Office Cleaning Singapore")]
    v = evaluate_retrieval("office cleaning Singapore", pages)
    assert v.verdict == "correct"
    assert 0.0 <= v.confidence <= 1.0
    assert v.assessments and v.assessments[0].condensed


def test_evaluate_incorrect_for_offtopic_pages():
    pages = [_Page("Adorable kittens and puppies photo gallery. Cute pets only.", "Cat pictures")]
    v = evaluate_retrieval("industrial flange gasket DN50", pages)
    assert v.verdict == "incorrect"


def test_evaluate_ambiguous_for_generic_vendor_page():
    pages = [_Page("We provide services in Singapore. Request a quotation by email. Contact sales.",
                   "Services")]
    v = evaluate_retrieval("office cleaning", pages)
    assert v.verdict == "ambiguous"


def test_evaluate_empty_pages_has_no_assessments():
    v = evaluate_retrieval("anything", [])
    assert v.assessments == []
    assert 0.0 <= v.confidence <= 1.0


def test_corrective_queries_broker_first():
    v = CorrectiveVerdict(verdict="incorrect", confidence=0.0, mean_relevance=0.0)
    qs = corrective_queries("obsolete DF13 connector", v)
    assert qs and qs[0].kind == "broker_operator"


def test_controller_irrelevant_pages_trigger_broker_pivot():
    class RecordingSearch:
        provider_name = "rec"
        search_source_tool = "mock"

        def __init__(self):
            self.queries: list[str] = []

        async def search(self, query, location, language, limit):
            self.queries.append(query)
            return SearchResultSet(
                query=query, location=location, provider="mock",
                results=[SearchResult(url=f"https://offtopic.example/p{len(self.queries)}",
                                      title="Cat pictures", snippet="kittens")],
                total_results=1,
            )

    class CatFetch:
        provider_name = "mock"
        fetch_source_tool = "mock"

        async def fetch(self, urls, output_format="markdown", include_links=True):
            return FetchResultSet(
                results=[FetchResult(url=u, final_url=u, title="Cat pictures",
                                     text="Adorable kittens and puppies gallery. Nothing relevant here.",
                                     links=[], source_tool="mock") for u in urls],
                provider="mock",
            )

    search = RecordingSearch()
    result = asyncio.run(Controller(
        search_provider=search, fetch_provider=CatFetch(), state_dir=None, persist=False,
    ).run("industrial flange gasket DN50", mode="product_exact_price"))

    assert result.metrics["crag_verdict"] == "incorrect"
    assert result.metrics["corrective_searches"] >= 1
    # A broaden/broker corrective query was emitted (not just the local templates).
    assert any("rochester" in q.lower() or "alternative" in q.lower()
               or "cross reference" in q.lower() for q in search.queries)
    # The controller did not fabricate an answer from irrelevant pages.
    assert result.validated_candidates == []


def test_relevant_run_does_not_pivot():
    result = asyncio.run(Controller(
        search_provider=MockSearchProvider(), fetch_provider=MockFetchProvider(),
        state_dir=None, persist=False,
    ).run("office cleaning Singapore", mode="auto"))
    assert result.metrics["crag_verdict"] in ("correct", "ambiguous")
    assert result.metrics["corrective_searches"] == 0
