"""T-2.1 acceptance: the judge gate runs before the ledger writer persists a page.

A low-authority page is rejected (dropped, not stored), a borderline page is
flagged (stored with a marker + reduced confidence), and an authoritative
relevant page is accepted. Without a judge the FetchService is unchanged.
"""

from __future__ import annotations

import asyncio

from spider_qwen.evidence.ledger import EvidenceLedger
from spider_qwen.tools.fetch_service import FetchService, MockFetchProvider
from spider_qwen.tools.page_judge import PageJudge
from spider_qwen.tools.provider_types import SearchResult, SearchResultSet


def _service(fixtures, **kw):
    ledger = EvidenceLedger("run_test", None)
    svc = FetchService(MockFetchProvider(fixtures=fixtures), ledger,
                       judge=PageJudge(current_year=2026), **kw)
    return svc, ledger


def test_rejected_low_authority_page_not_persisted():
    good = "https://www.ti.com/lm358"
    bad = "https://www.aliexpress.com/item/1.html"
    fixtures = {
        good: {"title": "LM358 Datasheet",
               "text": "LM358 operational amplifier datasheet 2025. Pricing and stock."},
        bad: {"title": "LM358 lot",
              "text": "LM358 operational amplifier lot 2025 buy now."},
    }
    svc, ledger = _service(fixtures, query="LM358 operational amplifier")
    rs = asyncio.run(svc.fetch([good, bad]))

    kept = [r.final_url or r.url for r in rs.results]
    assert good in kept
    assert bad not in kept  # rejected -> dropped from results
    stored = [it.url for it in ledger.items()]
    assert good in stored
    assert bad not in stored  # rejected -> never written to the ledger
    assert any("aliexpress" in e.get("url", "") for e in rs.errors)
    assert svc.rejected == 1 and svc.flagged == 0


def test_flagged_page_persisted_with_marker_and_reduced_confidence():
    url = "https://www.rochesterelectronics.com/lm358"
    fixtures = {url: {"title": "LM358 stock",
                      "text": "LM358 operational amplifier in stock. Request a quote 2025."}}
    svc, ledger = _service(fixtures, query="LM358 operational amplifier")
    rs = asyncio.run(svc.fetch([url]))

    assert len(rs.results) == 1
    item = ledger.items()[0]
    assert item.metadata.get("gate_status") == "flagged"
    assert item.metadata.get("judge", {}).get("verdict") == "flag"
    assert item.confidence < 0.6  # flagged evidence is down-weighted
    assert svc.flagged == 1 and svc.rejected == 0


def test_accepted_page_carries_gate_metadata():
    url = "https://www.mouser.sg/lm358"
    fixtures = {url: {"title": "LM358",
                      "text": "LM358 operational amplifier. In stock, pricing 2025."}}
    svc, ledger = _service(fixtures, query="LM358 operational amplifier")
    asyncio.run(svc.fetch([url]))
    item = ledger.items()[0]
    assert item.metadata.get("gate_status") == "accepted"
    assert item.metadata.get("judge", {}).get("verdict") == "accept"


def test_empty_text_low_authority_page_is_judged_and_rejected():
    # An image-only / no-extract page (text == "") from a marketplace host must
    # still be gated, not silently stored at the default confidence.
    url = "https://www.aliexpress.com/item/2.html"
    fixtures = {url: {"title": "LM358", "text": ""}}
    svc, ledger = _service(fixtures, query="LM358 operational amplifier")
    rs = asyncio.run(svc.fetch([url]))
    assert rs.results == []
    assert ledger.items() == []
    assert svc.rejected == 1


def test_no_judge_is_backward_compatible():
    url = "https://www.aliexpress.com/item/1.html"
    fixtures = {url: {"title": "x", "text": "LM358 2025"}}
    ledger = EvidenceLedger("run_test", None)
    svc = FetchService(MockFetchProvider(fixtures=fixtures), ledger)  # no judge
    rs = asyncio.run(svc.fetch([url]))
    assert len(rs.results) == 1
    assert len(ledger.items()) == 1
    assert "gate_status" not in ledger.items()[0].metadata
    assert svc.rejected == 0 and svc.flagged == 0


def test_controller_does_not_store_rejected_page():
    from spider_qwen.agent.controller import Controller

    good = "https://www.ti.com/lm358"
    bad = "https://www.aliexpress.com/item/1.html"

    class _FixedSearch:
        provider_name = "mock"
        search_source_tool = "mock"

        async def search(self, query, location, language, limit):
            results = [
                SearchResult(url=u, title=u,
                             snippet="LM358 operational amplifier supplier pricing",
                             rank=i, source_tool="mock")
                for i, u in enumerate([good, bad])
            ]
            return SearchResultSet(query=query, location=location, results=results,
                                   total_results=2, provider="mock")

    fixtures = {
        good: {"title": "LM358 Datasheet",
               "text": "LM358 operational amplifier datasheet 2025. Public pricing S$2 per unit. "
                       "MOQ 100 units. For volume orders email sales@ti.com or call +65 6123 4567."},
        bad: {"title": "LM358 lot",
              "text": "LM358 operational amplifier lot 2025 buy now S$1 each. Email sales@aliexpress.com."},
    }

    controller = Controller(
        search_provider=_FixedSearch(),
        fetch_provider=MockFetchProvider(fixtures=fixtures),
        page_judge=PageJudge(current_year=2026),
        state_dir=None, persist=False,
    )
    result = asyncio.run(controller.run("LM358 operational amplifier", target_country="Singapore"))

    assert result.metrics.get("pages_rejected", 0) >= 1
    for cand in result.validated_candidates:
        for ref in cand.get("evidence_refs", []):
            assert "aliexpress" not in (ref.get("url", "") or "")
