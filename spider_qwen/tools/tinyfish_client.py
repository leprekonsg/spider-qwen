"""Minimal async TinyFish Search + Fetch client (httpx).

Search:  GET  {search_base_url}            params: query, location, language, page
Fetch:   POST {fetch_base_url}             body: urls (<=10), format, links, image_links
Auth:    X-API-Key header

Only Search and Fetch are used (no Agent, no Browser) per v1 scope. Fetch caps
at 10 URLs/request and returns per-URL errors without failing the whole batch.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Iterable

import httpx

DEFAULT_SEARCH_URL = "https://api.search.tinyfish.ai"
DEFAULT_FETCH_URL = "https://api.fetch.tinyfish.ai"
FETCH_MAX_URLS = 10


class TinyFishError(Exception):
    """TinyFish client failure (HTTP, transport, or invalid response)."""


class TinyFishClient:
    def __init__(
        self,
        api_key: str,
        *,
        search_base_url: str = DEFAULT_SEARCH_URL,
        fetch_base_url: str = DEFAULT_FETCH_URL,
        fetch_batch_size: int = FETCH_MAX_URLS,
        timeout_seconds: float = 60.0,
        max_retries: int = 2,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise TinyFishError("TINYFISH_API_KEY is required to use the TinyFish client")
        self.api_key = api_key
        self.search_base_url = search_base_url.rstrip("/")
        self.fetch_base_url = fetch_base_url.rstrip("/")
        self.fetch_batch_size = max(1, min(fetch_batch_size, FETCH_MAX_URLS))
        self.max_retries = max(0, max_retries)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout_seconds, headers=self._headers()
        )

    def _headers(self) -> dict[str, str]:
        return {
            "X-API-Key": self.api_key,
            "Accept": "application/json",
            "User-Agent": "spider-qwen/0.1",
        }

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> "TinyFishClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def search(
        self,
        query: str,
        location: str | None = None,
        language: str = "en",
        page: int = 0,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"query": query, "page": max(0, min(page, 10))}
        if location:
            params["location"] = location
        if language:
            params["language"] = language
        return await self._request("GET", self.search_base_url, params=params)

    async def fetch(
        self,
        urls: Iterable[str],
        output_format: str = "markdown",
        include_links: bool = True,
        include_image_links: bool = False,
    ) -> dict[str, Any]:
        clean = _dedupe([u for u in urls if u])
        combined: dict[str, Any] = {"results": [], "errors": []}
        for batch in _chunks(clean, self.fetch_batch_size):
            payload = {
                "urls": batch,
                "format": output_format,
                "links": include_links,
                "image_links": include_image_links,
            }
            resp = await self._request("POST", self.fetch_base_url, json_payload=payload)
            combined["results"].extend(resp.get("results", []))
            combined["errors"].extend(resp.get("errors", []))
        return combined

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = await self._client.request(
                    method, url, params=params, json=json_payload
                )
                if resp.status_code == 429 or 500 <= resp.status_code < 600:
                    if attempt < self.max_retries:
                        await asyncio.sleep(min(15.0, 2**attempt))
                        continue
                if resp.status_code >= 400:
                    raise TinyFishError(
                        f"TinyFish HTTP {resp.status_code} on {url}: {resp.text[:200]}"
                    )
                return resp.json()
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    await asyncio.sleep(min(15.0, 2**attempt))
                    continue
                raise TinyFishError(f"TinyFish transport error on {url}: {exc}") from exc
        raise TinyFishError(f"TinyFish request failed on {url}: {last_exc}")


def from_env() -> TinyFishClient:
    return TinyFishClient(
        api_key=os.getenv("TINYFISH_API_KEY", ""),
        search_base_url=os.getenv("TINYFISH_SEARCH_BASE_URL", DEFAULT_SEARCH_URL),
        fetch_base_url=os.getenv("TINYFISH_FETCH_BASE_URL", DEFAULT_FETCH_URL),
        fetch_batch_size=int(os.getenv("TINYFISH_FETCH_BATCH_SIZE", str(FETCH_MAX_URLS))),
        timeout_seconds=float(os.getenv("TINYFISH_TIMEOUT_SECONDS", "60")),
        max_retries=int(os.getenv("TINYFISH_MAX_RETRIES", "2")),
    )


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _chunks(items: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]
