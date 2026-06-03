"""T-5.2: Wayback CDX temporal sourcing.

On a dead/404 URL the Wayback recoverer queries the CDX API
(`filter=statuscode:200`), fetches the most recent archived snapshot, and the
FetchService records it as a `wayback_cdx` evidence item. Throttle >=1s between
requests, exponential backoff on 429, circuit-breaker on sustained 429. All HTTP
is injected, so these tests are network-free and deterministic.
"""

from __future__ import annotations

import asyncio
import json

from spider_qwen.evidence.ledger import EvidenceLedger
from spider_qwen.serendipity.wayback import HttpResponse, RecoveredSnapshot, WaybackClient
from spider_qwen.tools.fetch_service import FetchService, MockFetchProvider
from spider_qwen.tools.page_judge import PageJudge

DEAD_URL = "https://example.sg/df13-6p-1.25dsa"
ARCHIVE_TS = "20200615120000"
SNAPSHOT_TEXT = "DF13-6P-1.25DSA Hirose 6-position connector. In stock 2019. Datasheet available."


def _cdx_body(url: str = DEAD_URL) -> str:
    # CDX json: header row first, then one row per snapshot (oldest + newest here).
    return json.dumps(
        [
            ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"],
            ["sg,example)/df13", "20180101000000", url, "text/html", "200", "AAA", "100"],
            ["sg,example)/df13", ARCHIVE_TS, url, "text/html", "200", "BBB", "200"],
        ]
    )


async def _noop_sleep(_seconds: float) -> None:
    return None


def _routed_http(*, cdx: HttpResponse | None = None, snapshot: HttpResponse | None = None,
                 calls: list[str] | None = None):
    cdx = cdx if cdx is not None else HttpResponse(200, _cdx_body())
    snapshot = snapshot if snapshot is not None else HttpResponse(200, SNAPSHOT_TEXT)

    async def http_get(url: str) -> HttpResponse:
        if calls is not None:
            calls.append(url)
        return cdx if "cdx" in url else snapshot

    return http_get


def _client(http_get, **kw) -> WaybackClient:
    kw.setdefault("sleep", _noop_sleep)
    kw.setdefault("monotonic", lambda: 0.0)
    return WaybackClient(http_get, **kw)


def test_cdx_query_url_filters_status_200():
    q = _client(_routed_http()).cdx_query_url(DEAD_URL)
    assert "cdx" in q
    assert "output=json" in q
    assert "filter=statuscode:200" in q
    assert "example.sg" in q  # original url encoded into the query


def test_recover_returns_most_recent_archived_snapshot():
    calls: list[str] = []
    snap = asyncio.run(_client(_routed_http(calls=calls)).recover(DEAD_URL))
    assert snap is not None
    assert snap.original_url == DEAD_URL
    assert ARCHIVE_TS in snap.archive_url  # newest of the two snapshots is chosen
    assert "id_" in snap.archive_url       # raw archived-content form (no IA banner)
    assert "DF13" in snap.text
    assert sum(1 for c in calls if "cdx" in c) == 1   # exactly one CDX query
    assert sum(1 for c in calls if "/web/" in c) == 1  # exactly one snapshot fetch


def test_recover_returns_none_when_no_snapshots():
    empty = HttpResponse(200, json.dumps([["urlkey", "timestamp", "original", "statuscode"]]))
    assert asyncio.run(_client(_routed_http(cdx=empty)).recover(DEAD_URL)) is None


def test_throttle_enforces_min_interval_between_requests():
    waits: list[float] = []

    async def rec_sleep(d: float) -> None:
        waits.append(d)

    client = WaybackClient(_routed_http(), sleep=rec_sleep, monotonic=lambda: 0.0, min_interval=1.0)
    asyncio.run(client.recover(DEAD_URL))
    # Two requests; with the fake clock frozen, the second must wait ~1s -> >=1 req/s.
    assert any(w >= 1.0 for w in waits)


def test_exponential_backoff_retries_on_429():
    waits: list[float] = []

    async def rec_sleep(d: float) -> None:
        waits.append(d)

    seq = iter([HttpResponse(429, ""), HttpResponse(200, _cdx_body()), HttpResponse(200, SNAPSHOT_TEXT)])

    async def http_get(_url: str) -> HttpResponse:
        return next(seq)

    client = WaybackClient(http_get, sleep=rec_sleep, monotonic=lambda: 0.0,
                           min_interval=0.0, max_retries=2, base_backoff=1.0)
    snap = asyncio.run(client.recover(DEAD_URL))
    assert snap is not None          # recovered after retrying past the 429
    assert any(w >= 1.0 for w in waits)  # a backoff sleep happened (min_interval=0 -> not throttle)


def test_circuit_breaker_opens_on_sustained_429():
    calls: list[str] = []

    async def http_get(url: str) -> HttpResponse:
        calls.append(url)
        return HttpResponse(429, "")

    client = WaybackClient(http_get, sleep=_noop_sleep, monotonic=lambda: 0.0,
                           min_interval=0.0, max_retries=2, breaker_threshold=3)
    assert asyncio.run(client.recover(DEAD_URL)) is None
    assert client.breaker_open is True
    n_after_first = len(calls)
    assert n_after_first == 3  # initial + 2 retries before tripping

    assert asyncio.run(client.recover(DEAD_URL)) is None
    assert len(calls) == n_after_first  # breaker short-circuits: no further requests


def test_fetch_service_404_falls_back_to_wayback_with_evidence_ref():
    ledger = EvidenceLedger("run_test", None)
    provider = MockFetchProvider(fixtures={DEAD_URL: {"status": 404}})
    svc = FetchService(provider, ledger, wayback=_client(_routed_http()))
    rs = asyncio.run(svc.fetch([DEAD_URL]))

    assert len(rs.results) == 1
    rec = rs.results[0]
    assert rec.source_tool == "wayback_cdx"
    assert rec.evidence_ref is not None

    item = ledger.get(rec.evidence_ref.ledger_id)
    assert item is not None
    assert item.source_tool == "wayback_cdx"
    assert item.metadata.get("recovered_from") == DEAD_URL
    assert "id_" in item.metadata.get("archive_url", "")
    assert svc.recovered == 1


def test_recover_returns_none_when_cdx_rows_are_malformed():
    # A truncated CDX row (fewer columns than the header) must degrade to None,
    # never raise IndexError out of recover().
    malformed = HttpResponse(200, json.dumps([
        ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"],
        ["sg,example)/df13"],  # truncated: no timestamp/original
    ]))
    assert asyncio.run(_client(_routed_http(cdx=malformed)).recover(DEAD_URL)) is None


def test_circuit_breaker_accumulates_sustained_429_across_calls():
    # The breaker must trip on sustained 429s even when threshold > max_retries+1
    # (the default 5 > 4 can never be reached inside one recover()). The 429 count
    # therefore accumulates across recover() calls and only a 200 resets it.
    calls: list[str] = []

    async def http_get(url: str) -> HttpResponse:
        calls.append(url)
        return HttpResponse(429, "")

    client = WaybackClient(http_get, sleep=_noop_sleep, monotonic=lambda: 0.0,
                           min_interval=0.0, max_retries=1, breaker_threshold=3)
    assert asyncio.run(client.recover(DEAD_URL)) is None
    assert client.breaker_open is False  # only 2 consecutive 429s so far (< 3)
    assert asyncio.run(client.recover(DEAD_URL)) is None
    assert client.breaker_open is True   # 3rd consecutive 429 across calls trips it
    assert len(calls) == 3               # 2 (url1: initial+retry) + 1 (url2: trips, then short-circuits)


def test_judge_rejected_url_is_not_recovered_by_wayback():
    # A page the judge gate rejects (low-authority) must NOT be re-introduced via
    # Wayback recovery -- that would bypass the gate and re-persist excluded content.
    class _RecordingWayback:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def recover(self, url: str):
            self.calls.append(url)
            return RecoveredSnapshot(original_url=url, archive_url=f"{url}#id_", timestamp="x", text="low authority")

    bad = "https://www.aliexpress.com/item/1.html"
    ledger = EvidenceLedger("run_test", None)
    wb = _RecordingWayback()
    svc = FetchService(
        MockFetchProvider(fixtures={bad: {"title": "LM358 lot", "text": "LM358 lot 2025 buy now."}}),
        ledger, judge=PageJudge(current_year=2026), query="LM358 operational amplifier", wayback=wb,
    )
    rs = asyncio.run(svc.fetch([bad]))

    assert wb.calls == []                 # recovery never attempted on a judge rejection
    assert svc.recovered == 0
    assert rs.results == []
    assert all(it.source_tool != "wayback_cdx" for it in ledger.items())


def test_fetch_service_without_wayback_does_not_recover():
    ledger = EvidenceLedger("run_test", None)
    provider = MockFetchProvider(fixtures={DEAD_URL: {"status": 404}})
    svc = FetchService(provider, ledger)  # no wayback recoverer
    rs = asyncio.run(svc.fetch([DEAD_URL]))

    assert rs.results == []
    assert any(e.get("url") == DEAD_URL for e in rs.errors)
    assert ledger.items() == []
    assert svc.recovered == 0
