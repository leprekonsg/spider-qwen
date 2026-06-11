"""Cross-run page cache: a second run over the same pages must not re-buy
them. Hits skip the provider, consume no fetch budget, and still land in the
new run's ledger with cache provenance.
"""

from __future__ import annotations

import asyncio

from spider_qwen.agent.controller import Controller
from spider_qwen.tools.page_cache import PageCache
from spider_qwen.tools.provider_types import FetchResult

QUERY = "office cleaning Singapore"

OK_TEXT = (
    "We provide office cleaning services across Singapore for commercial "
    "buildings. Request a quotation by emailing sales@vendor.sg or call "
    "+65 6123 4567. Daily, weekly and post-renovation cleans available."
)


def _page(url: str, text: str = OK_TEXT) -> FetchResult:
    return FetchResult(url=url, final_url=url, title="Vendor Pte Ltd",
                       text=text, links=[url + "/contact"], source_tool="mock")


def test_put_get_roundtrip_and_canonical_key(tmp_path):
    cache = PageCache(tmp_path)
    assert cache.put(_page("https://www.vendor.sg/cleaning/")) is True
    # http/https, www, and trailing-slash variants collapse to one entry.
    hit = cache.get("http://vendor.sg/cleaning")
    assert hit is not None
    assert hit.text == OK_TEXT
    assert hit.links == ["https://www.vendor.sg/cleaning//contact"]


def test_miss_and_ttl_expiry(tmp_path):
    cache = PageCache(tmp_path, ttl_seconds=-1)  # everything is already stale
    cache.put(_page("https://vendor.sg/cleaning"))
    assert cache.get("https://vendor.sg/cleaning") is None
    assert PageCache(tmp_path).get("https://never-stored.sg/") is None


def test_non_ok_pages_are_never_cached(tmp_path):
    cache = PageCache(tmp_path)
    assert cache.put(_page("https://walled.sg/x",
                           "Access denied. Verify you are a human.")) is False
    assert cache.put(_page("https://blank.sg/x", "")) is False
    assert cache.get("https://walled.sg/x") is None


def _run(state_dir):
    controller = Controller(offline=True, state_dir=state_dir)
    return asyncio.run(controller.run(QUERY))


def test_second_run_hits_cache_and_spends_no_fetch_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("SPIDER_QWEN_PAGE_CACHE_ENABLED", "1")
    first = _run(tmp_path)
    assert first.metrics["page_cache"]["enabled"] is True
    assert first.metrics["page_cache"]["hits"] == 0
    assert first.budget["fetch_urls"] > 0

    second = _run(tmp_path)
    assert second.metrics["page_cache"]["hits"] > 0
    # Every page the first run bought is free the second time.
    assert second.budget["fetch_urls"] < first.budget["fetch_urls"]
    # The cached pages still produced evidence-backed candidates in THIS run.
    assert second.validated_candidates
    assert second.evidence_refs


def test_cached_page_evidence_carries_cache_provenance(tmp_path, monkeypatch):
    monkeypatch.setenv("SPIDER_QWEN_PAGE_CACHE_ENABLED", "1")
    _run(tmp_path)
    second = _run(tmp_path)
    from spider_qwen.evidence.ledger import EvidenceLedger

    ledger = EvidenceLedger.load(second.run_id, tmp_path)
    cached_rows = [i for i in ledger.items() if i.metadata.get("page_cache")]
    assert cached_rows
    assert all(row.metadata["page_cache"]["hit"] is True for row in cached_rows)
    assert all("fetched_at" in row.metadata["page_cache"] for row in cached_rows)


def test_flag_off_means_cold_runs_and_no_cache_dir(tmp_path):
    first = _run(tmp_path)
    second = _run(tmp_path)
    assert first.metrics["page_cache"] == {"enabled": False, "hits": 0, "misses": 0}
    assert second.budget["fetch_urls"] == first.budget["fetch_urls"]
    assert not (tmp_path / "page_cache").exists()
