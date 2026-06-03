"""Search service: wraps a SearchProvider, records evidence, enforces budget.

Providers:
- TinyFishSearchProvider  (primary, ranked URL discovery)
- QwenMcpSearchProvider   (provider abstraction; MCP/responses backend injected)
- MockSearchProvider      (offline deterministic fixtures for tests/demos)
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Awaitable, Callable

from .fetch_service import _looks_like_obsolete_part_topic
from .provider_types import SearchResult, SearchResultSet
from .tinyfish_client import TinyFishClient, from_env as tinyfish_from_env

if TYPE_CHECKING:  # avoid import cycle; tracker is duck-typed at runtime
    from ..agent.budget import BudgetTracker
    from ..evidence.ledger import EvidenceLedger


class SearchProviderError(Exception):
    pass


class TinyFishSearchProvider:
    provider_name = "tinyfish"
    search_source_tool = "tinyfish_search"
    rate_limited = True  # consumes the live TinyFish free-tier search quota

    def __init__(self, client: TinyFishClient | None = None) -> None:
        self.client = client or tinyfish_from_env()

    async def search(
        self, query: str, location: str | None, language: str, limit: int
    ) -> SearchResultSet:
        raw = await self.client.search(query=query, location=location, language=language)
        results: list[SearchResult] = []
        for rank, item in enumerate(raw.get("results", [])[:limit]):
            url = item.get("url")
            if not url:
                continue
            results.append(
                SearchResult(
                    url=url,
                    title=item.get("title"),
                    snippet=item.get("snippet") or item.get("description") or "",
                    rank=rank,
                    source_tool="tinyfish_search",
                )
            )
        return SearchResultSet(
            query=query,
            location=location,
            results=results,
            total_results=raw.get("total_results", len(results)),
            provider="tinyfish",
        )


class QwenMcpSearchProvider:
    """Qwen search via MCP / Model Studio responses backend.

    Qwen Code has no built-in web_search; it is MCP-based. This provider keeps
    the abstraction in place. Wire a real backend by passing a coroutine
    `backend(query, location, language, limit) -> SearchResultSet`. Without one
    it raises, so callers fall back to TinyFish rather than silently no-op.
    """

    provider_name = "qwen_mcp"
    search_source_tool = "mcp_search"
    rate_limited = True  # consumes the live Qwen MCP / Model Studio quota

    def __init__(
        self,
        backend: Callable[[str, str | None, str, int], Awaitable[SearchResultSet]] | None = None,
    ) -> None:
        self.backend = backend

    async def search(
        self, query: str, location: str | None, language: str, limit: int
    ) -> SearchResultSet:
        if self.backend is None:
            raise SearchProviderError(
                "QwenMcpSearchProvider has no MCP backend configured. "
                "Inject a backend or set SPIDER_QWEN_SEARCH_PROVIDER=tinyfish."
            )
        return await self.backend(query, location, language, limit)


class MockSearchProvider:
    """Deterministic offline provider. Returns fixtures by query, else synthesizes."""

    provider_name = "mock"
    search_source_tool = "mock"
    rate_limited = False  # offline fixtures hit no external quota; never throttle

    def __init__(self, fixtures: dict[str, list[dict]] | None = None) -> None:
        self.fixtures = fixtures or {}

    async def search(
        self, query: str, location: str | None, language: str, limit: int
    ) -> SearchResultSet:
        raw = self.fixtures.get(query)
        if raw is None:
            slug = "".join(c if c.isalnum() else "-" for c in query.lower()).strip("-")[:40]
            if _looks_like_obsolete_part_topic(query):
                # T-8.2: seed an obsolete-part run with a datasheet page (cross-refs +
                # lifecycle) and a broker page, so the --serendipity sidecar has real
                # S1 (graph) and S2 (long-tail/broker) material offline.
                raw = [
                    {"url": f"https://datasheet-archive.example/{slug}",
                     "title": f"Datasheet & cross-reference - {query}",
                     "snippet": f"Obsolete-part cross-reference and lifecycle data for {query}."},
                    {"url": f"https://rochester-electronics.example/{slug}",
                     "title": f"Broker stock - {query}",
                     "snippet": f"Long-tail / last-time-buy broker stock for {query}."},
                    {"url": f"https://example-vendor-1.sg/{slug}",
                     "title": f"Vendor 1 - {query}",
                     "snippet": f"Provider 1 for {query}. Request a quotation via our contact page."},
                ]
            else:
                raw = [
                    {
                        "url": f"https://example-vendor-{i}.sg/{slug}",
                        "title": f"Vendor {i} - {query}",
                        "snippet": f"Provider {i} for {query}. Request a quotation via our contact page.",
                    }
                    for i in range(1, min(limit, 5) + 1)
                ]
        results = [
            SearchResult(
                url=item["url"],
                title=item.get("title"),
                snippet=item.get("snippet", ""),
                rank=rank,
                source_tool="mock",
            )
            for rank, item in enumerate(raw[:limit])
        ]
        return SearchResultSet(
            query=query, location=location, results=results,
            total_results=len(results), provider="mock",
        )


class SearchService:
    """Budget-aware, evidence-recording wrapper over any SearchProvider."""

    def __init__(
        self,
        provider: object,
        ledger: "EvidenceLedger",
        tracker: "BudgetTracker | None" = None,
        tracer: object | None = None,
    ) -> None:
        self.provider = provider
        self.ledger = ledger
        self.tracker = tracker
        self.tracer = tracer

    async def search(
        self, query: str, location: str | None = None, language: str = "en", limit: int = 10
    ) -> SearchResultSet:
        if self.tracker is not None:
            self.tracker.consume_search()
        source_tool = getattr(self.provider, "search_source_tool", "mcp_search")
        try:
            result_set = await self.provider.search(query, location, language, limit)
        except Exception as exc:
            if self.tracer is not None:
                self.tracer.record(step="search", tool=source_tool, status="error",
                                   input_count=1, output_count=0, error=str(exc))
            raise
        for r in result_set.results:
            r.evidence_ref = self.ledger.record(
                source_tool=source_tool,
                url=r.url,
                title=r.title,
                snippet=r.snippet,
                confidence=0.4,
                metadata={"query": query, "rank": r.rank, "provider": result_set.provider},
            )
        if self.tracer is not None:
            self.tracer.record(step="search", tool=source_tool, status="success",
                               input_count=1, output_count=len(result_set.results))
        return result_set


def build_search_provider(name: str | None = None, *, fixtures: dict | None = None) -> object:
    """Construct a search provider by name (env SPIDER_QWEN_SEARCH_PROVIDER)."""
    name = (name or os.getenv("SPIDER_QWEN_SEARCH_PROVIDER", "tinyfish")).lower()
    if name == "mock":
        return MockSearchProvider(fixtures=fixtures)
    if name == "qwen_mcp":
        return QwenMcpSearchProvider()
    return TinyFishSearchProvider()
