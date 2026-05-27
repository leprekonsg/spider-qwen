"""Fetch service: wraps a FetchProvider, records evidence, enforces fetch budget.

Providers:
- TinyFishFetchProvider          (primary, multi-URL clean content + links)
- QwenWebExtractorFetchProvider  (single-page fallback; see qwen_web_extractor)
- MockFetchProvider              (offline deterministic fixtures)
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from .provider_types import FetchResult, FetchResultSet
from .tinyfish_client import TinyFishClient, from_env as tinyfish_from_env

if TYPE_CHECKING:
    from ..agent.budget import BudgetTracker
    from ..evidence.ledger import EvidenceLedger


class TinyFishFetchProvider:
    provider_name = "tinyfish"
    fetch_source_tool = "tinyfish_fetch"

    def __init__(self, client: TinyFishClient | None = None) -> None:
        self.client = client or tinyfish_from_env()

    async def fetch(
        self, urls: list[str], output_format: str = "markdown", include_links: bool = True
    ) -> FetchResultSet:
        raw = await self.client.fetch(urls, output_format=output_format, include_links=include_links)
        results = [
            FetchResult(
                url=p.get("url", ""),
                final_url=p.get("final_url"),
                title=p.get("title"),
                text=p.get("text", ""),
                links=p.get("links", []) or [],
                language=p.get("language"),
                metadata={k: v for k, v in p.items() if k not in {"url", "final_url", "title", "text", "links", "language"}},
                source_tool="tinyfish_fetch",
            )
            for p in raw.get("results", [])
        ]
        return FetchResultSet(results=results, errors=raw.get("errors", []), provider="tinyfish")


class MockFetchProvider:
    provider_name = "mock"
    fetch_source_tool = "mock"

    def __init__(self, fixtures: dict[str, dict] | None = None) -> None:
        self.fixtures = fixtures or {}

    async def fetch(
        self, urls: list[str], output_format: str = "markdown", include_links: bool = True
    ) -> FetchResultSet:
        results: list[FetchResult] = []
        for url in urls:
            fx = self.fixtures.get(url)
            if fx is not None:
                results.append(FetchResult(url=url, final_url=url, source_tool="mock", **fx))
            else:
                # Derive page topic from the URL path so the deterministic mock
                # exercises service-match/extraction the way a real page would.
                topic = urlparse(url).path.strip("/").replace("-", " ").replace("_", " ") or "professional services"
                host = (urlparse(url).netloc or "example-vendor.sg").lower()
                if host.startswith("www."):
                    host = host[4:]
                email = f"sales@{host}"
                lower_topic = topic.lower()
                if "missing price" in lower_topic or "price missing" in lower_topic:
                    text = (
                        f"We supply {topic} in Singapore. Product catalogue available on request. "
                        f"For enquiries email {email} or call +65 6123 4567."
                    )
                elif "conflicting price" in lower_topic or "conflicting prices" in lower_topic:
                    text = (
                        f"We supply {topic} in Singapore. Price is S$10 per unit. "
                        f"Elsewhere this page lists S$200 per unit. Email {email}."
                    )
                elif "rate card" in lower_topic:
                    text = (
                        f"We supply {topic} in Singapore. Download our rate card for public pricing. "
                        f"Email {email} for volume terms."
                    )
                elif _looks_like_product_topic(topic):
                    text = (
                        f"We supply {topic} in Singapore. Public pricing starts at S$129 per unit. "
                        f"MOQ 50 units. For volume orders email {email} or call +65 6123 4567."
                    )
                else:
                    text = (
                        f"We provide {topic} in Singapore. "
                        f"Request a quotation by emailing {email} "
                        "or call +65 6123 4567. Contact us for pricing."
                    )
                results.append(
                    FetchResult(
                        url=url,
                        final_url=url,
                        title=_title_from_host(host),
                        text=text,
                        links=[url.rstrip("/") + "/contact", url.rstrip("/") + "/request-a-quote"],
                        source_tool="mock",
                    )
                )
        return FetchResultSet(results=results, errors=[], provider="mock")


class FetchService:
    """Budget-aware, evidence-recording wrapper over any FetchProvider."""

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

    async def fetch(
        self, urls: list[str], output_format: str = "markdown", include_links: bool = True
    ) -> FetchResultSet:
        urls = [u for u in urls if u]
        if not urls:
            return FetchResultSet(provider=getattr(self.provider, "provider_name", "fetch"))
        if self.tracker is not None:
            allowed = self.tracker.consume_fetch(len(urls))
            urls = urls[:allowed]
        source_tool = getattr(self.provider, "fetch_source_tool", "tinyfish_fetch")
        result_set = await self.provider.fetch(urls, output_format, include_links)
        for p in result_set.results:
            p.evidence_ref = self.ledger.record(
                source_tool=source_tool,
                url=p.url,
                final_url=p.final_url,
                title=p.title,
                snippet=(p.text or "")[:500],
                text=p.text,
                language=p.language,
                confidence=0.6,
                metadata={"links": p.links[:20], "provider": result_set.provider},
            )
        if self.tracer is not None:
            self.tracer.record(step="fetch", tool=source_tool, status="success",
                               input_count=len(urls), output_count=len(result_set.results))
        return result_set


def build_fetch_provider(name: str | None = None, *, fixtures: dict | None = None) -> object:
    """Construct a fetch provider by name (env SPIDER_QWEN_FETCH_PROVIDER)."""
    name = (name or os.getenv("SPIDER_QWEN_FETCH_PROVIDER", "tinyfish")).lower()
    if name == "mock":
        return MockFetchProvider(fixtures=fixtures)
    if name == "qwen_web_extractor":
        from .qwen_web_extractor import QwenWebExtractorFetchProvider

        return QwenWebExtractorFetchProvider()
    return TinyFishFetchProvider()


def _looks_like_product_topic(topic: str) -> bool:
    text = (topic or "").lower()
    product_terms = (
        "chair", "chairs", "paper", "keyboard", "keyboards", "monitor", "monitors",
        "laptop", "laptops", "desk", "desks", "printer", "printers", "cartridge",
        "units", "pcs", "pieces", "supplies", "uniform", "uniforms", "cable", "cables",
        "sensor", "sensors", "toner", "table", "tables", "ppe", "kits",
    )
    return any(term in text for term in product_terms)


def _title_from_host(host: str) -> str:
    label = (host or "example-vendor").split(".", 1)[0]
    return label.replace("-", " ").title() + " Pte Ltd"
