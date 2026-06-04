"""T-5.2: Wayback CDX temporal sourcing.

When a distributor / datasheet URL is dead (404, blocked), recover the most
recent archived copy from the Internet Archive:

    1. query the free CDX API filtered to ``statuscode:200`` snapshots;
    2. pick the newest snapshot timestamp;
    3. fetch its raw archived content (``.../web/<ts>id_/<original>``).

The Archive now enforces ~60 requests/min and hour-long IP blocks, so every
request is throttled to >=1/sec, retried with exponential backoff on HTTP 429,
and a circuit-breaker trips on sustained 429s (after which ``recover`` short-
circuits to ``None`` instead of hammering a blocked IP).

All I/O goes through an injected ``http_get`` seam (``url -> HttpResponse``), so
the recoverer is fully deterministic and network-free under test. The default
seam uses httpx and is only constructed when no seam is supplied.
"""

from __future__ import annotations

import json
import time
from typing import Awaitable, Callable, NamedTuple
from urllib.parse import quote

from pydantic import BaseModel

CDX_BASE = "http://web.archive.org/cdx/search/cdx"
SNAPSHOT_BASE = "https://web.archive.org/web"


class HttpResponse(NamedTuple):
    status: int
    text: str


HttpGet = Callable[[str], Awaitable[HttpResponse]]


class WaybackError(Exception):
    """Wayback recovery failure."""


class WaybackCircuitOpen(WaybackError):
    """Raised once the breaker trips after sustained 429s; no request is made."""


class RecoveredSnapshot(BaseModel):
    original_url: str
    archive_url: str
    timestamp: str
    text: str


async def _default_http_get(url: str) -> HttpResponse:
    # Imported lazily so the offline default never requires httpx to be importable
    # until a live recovery is actually attempted.
    import httpx

    async with httpx.AsyncClient(
        timeout=30.0, headers={"User-Agent": "spider-qwen/0.1"}, follow_redirects=True
    ) as client:
        resp = await client.get(url)
        return HttpResponse(resp.status_code, resp.text)


class WaybackClient:
    """Throttled, backoff-and-breaker Wayback CDX recoverer."""

    def __init__(
        self,
        http_get: HttpGet | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        monotonic: Callable[[], float] | None = None,
        min_interval: float = 1.0,
        max_retries: int = 3,
        base_backoff: float = 1.0,
        max_backoff: float = 30.0,
        breaker_threshold: int = 5,
        cdx_base: str = CDX_BASE,
        snapshot_base: str = SNAPSHOT_BASE,
    ) -> None:
        import asyncio

        self._http_get = http_get or _default_http_get
        self._sleep = sleep or asyncio.sleep
        self._monotonic = monotonic or time.monotonic
        self._min_interval = max(0.0, min_interval)
        self._max_retries = max(0, max_retries)
        self._base_backoff = max(0.0, base_backoff)
        self._max_backoff = max(0.0, max_backoff)
        self._breaker_threshold = max(1, breaker_threshold)
        self._cdx_base = cdx_base
        self._snapshot_base = snapshot_base.rstrip("/")
        self._last_ts: float | None = None
        self._consec_429 = 0
        self._breaker_open = False

    @property
    def breaker_open(self) -> bool:
        return self._breaker_open

    def cdx_query_url(self, url: str, *, limit: int = 5) -> str:
        """Most-recent ``limit`` snapshots that returned HTTP 200 for ``url``."""
        # limit=-N asks CDX for the newest N rows; collapse=digest drops identical captures.
        return (
            f"{self._cdx_base}?url={quote(url, safe='')}&output=json"
            f"&filter=statuscode:200&collapse=digest&limit=-{limit}"
        )

    async def recover(self, url: str) -> RecoveredSnapshot | None:
        """Return the newest archived copy of ``url``, or ``None`` if unavailable."""
        try:
            cdx = await self._get_with_retry(self.cdx_query_url(url))
            if cdx.status != 200:
                return None
            snapshot = self._latest_snapshot(cdx.text)
            if snapshot is None:
                return None
            timestamp, original = snapshot
            archive_url = f"{self._snapshot_base}/{timestamp}id_/{original}"
            page = await self._get_with_retry(archive_url)
        except WaybackCircuitOpen:
            return None
        if page.status != 200 or not page.text.strip():
            return None
        return RecoveredSnapshot(
            original_url=url, archive_url=archive_url, timestamp=timestamp, text=page.text
        )

    def _latest_snapshot(self, body: str) -> tuple[str, str] | None:
        try:
            rows = json.loads(body)
        except (ValueError, TypeError):
            return None
        if not isinstance(rows, list) or len(rows) < 2:
            return None  # header only (or empty) -> no captures
        header = rows[0]
        try:
            ts_i, orig_i = header.index("timestamp"), header.index("original")
        except (ValueError, AttributeError):
            ts_i, orig_i = 1, 2  # CDX default column order
        # A third-party API can return truncated/short rows; drop any that cannot
        # supply both columns so a malformed capture degrades to None, not a crash.
        width = max(ts_i, orig_i)
        valid = [r for r in rows[1:] if isinstance(r, (list, tuple)) and len(r) > width]
        if not valid:
            return None
        # CDX timestamps are zero-padded YYYYMMDDhhmmss, so max() == most recent.
        best = max(valid, key=lambda r: r[ts_i])
        return str(best[ts_i]), str(best[orig_i])

    async def _get_with_retry(self, url: str) -> HttpResponse:
        # _consec_429 accumulates ACROSS requests and recover() calls by design: an IA
        # IP block is a sustained-429 condition that spans calls, and the breaker must
        # trip even when breaker_threshold > max_retries+1 (the default 5 > 4 can never
        # be reached within a single call). Only a successful 200 resets the count.
        if self._breaker_open:
            raise WaybackCircuitOpen("Wayback circuit breaker open after sustained 429s")
        resp = HttpResponse(0, "")
        for attempt in range(self._max_retries + 1):
            await self._throttle()
            resp = await self._http_get(url)
            if resp.status == 429:
                self._consec_429 += 1
                if self._consec_429 >= self._breaker_threshold:
                    self._breaker_open = True
                    raise WaybackCircuitOpen(
                        f"Wayback returned {self._consec_429} consecutive 429s; backing off"
                    )
                if attempt < self._max_retries:
                    await self._sleep(self._backoff(attempt))
                    continue
                return resp  # retries exhausted, still rate-limited
            self._consec_429 = 0
            return resp
        return resp

    async def _throttle(self) -> None:
        if self._last_ts is not None and self._min_interval > 0:
            wait = self._min_interval - (self._monotonic() - self._last_ts)
            if wait > 0:
                await self._sleep(wait)
        self._last_ts = self._monotonic()

    def _backoff(self, attempt: int) -> float:
        return min(self._max_backoff, self._base_backoff * (2 ** attempt))
