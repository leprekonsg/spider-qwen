"""Fetch service: wraps a FetchProvider, records evidence, enforces fetch budget.

Providers:
- TinyFishFetchProvider          (primary, multi-URL clean content + links)
- QwenWebExtractorFetchProvider  (single-page fallback; see qwen_web_extractor)
- MockFetchProvider              (offline deterministic fixtures)

T-5.2: an optional Wayback recoverer (``wayback=``) turns a dead/404 fetch into an
archived snapshot recorded as a ``wayback_cdx`` evidence item. Opt-in; the default
controller pipeline does not wire it (full discovery-layer integration is Phase 8).
"""

from __future__ import annotations

import os
import re
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
    rate_limited = True  # consumes the live TinyFish free-tier fetch quota

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
    rate_limited = False  # offline fixtures hit no external quota; never throttle

    def __init__(self, fixtures: dict[str, dict] | None = None) -> None:
        self.fixtures = fixtures or {}

    async def fetch(
        self, urls: list[str], output_format: str = "markdown", include_links: bool = True
    ) -> FetchResultSet:
        results: list[FetchResult] = []
        errors: list[dict] = []
        for url in urls:
            fx = self.fixtures.get(url)
            if fx is not None:
                status = fx.get("status")
                if isinstance(status, int) and status >= 400:
                    # Simulate a dead/blocked URL so the Wayback fallback (T-5.2) is exercised.
                    errors.append({"url": url, "status": status, "error": f"HTTP {status}"})
                    continue
                results.append(FetchResult(url=url, final_url=url, source_tool="mock",
                                           **{k: v for k, v in fx.items() if k != "status"}))
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
                elif _looks_like_obsolete_part_topic(topic):
                    # T-8.2: obsolete-part page with cross-reference + lifecycle prose
                    # so the --serendipity discovery sidecar has real graph/signal
                    # material offline. Phrasing matches the graph relation extractor.
                    base = _first_mock_mpn(topic)
                    text = (
                        f"{base} is an obsolete component and is end-of-life (EOL). "
                        f"PCN-2024-001 has been issued for {base}. "
                        f"{base} is superseded by {base}A. "
                        f"{base} cross-references {base}B. "
                        f"{base} cross-references {base}C. "
                        f"{base} is pin-compatible with {base}D. "
                        f"For long-tail or last-time-buy stock, contact a broker or email {email}."
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
        return FetchResultSet(results=results, errors=errors, provider="mock")


class FetchService:
    """Budget-aware, evidence-recording wrapper over any FetchProvider."""

    def __init__(
        self,
        provider: object,
        ledger: "EvidenceLedger",
        tracker: "BudgetTracker | None" = None,
        tracer: object | None = None,
        *,
        judge: object | None = None,
        query: str | None = None,
        wayback: object | None = None,
    ) -> None:
        self.provider = provider
        self.ledger = ledger
        self.tracker = tracker
        self.tracer = tracer
        # T-2.1: optional page judge gate (accept/flag/reject) run before persist.
        self.judge = judge
        self.query = query or ""
        # T-5.2: optional Wayback recoverer; recovers dead/404 fetches from the archive.
        self.wayback = wayback
        self.judged = 0
        self.rejected = 0
        self.flagged = 0
        self.recovered = 0

    async def fetch(
        self, urls: list[str], output_format: str = "markdown", include_links: bool = True
    ) -> FetchResultSet:
        from ..agent.tool_registry import ToolRegistry

        ToolRegistry.require_allowed("fetch")
        urls = [u for u in urls if u]
        if not urls:
            return FetchResultSet(provider=getattr(self.provider, "provider_name", "fetch"))
        if self.tracker is not None:
            allowed = self.tracker.consume_fetch(len(urls))
            urls = urls[:allowed]
        source_tool = getattr(self.provider, "fetch_source_tool", "tinyfish_fetch")
        result_set = await self.provider.fetch(urls, output_format, include_links)
        kept: list[FetchResult] = []
        for p in result_set.results:
            verdict = None
            if self.judge is not None:
                # Judge every page, including empty-text ones: an image-only / no-
                # extract page from a low-authority host must not slip past the gate.
                verdict = self.judge.judge(
                    url=p.url, final_url=p.final_url, title=p.title,
                    text=p.text or "", query=self.query, prior_items=self.ledger.items(),
                )
                self.judged += 1
                if verdict.verdict == "reject":
                    # Low-authority / off-topic: never written to the ledger.
                    self.rejected += 1
                    result_set.errors.append({
                        "url": p.final_url or p.url,
                        "error": f"rejected by page judge: {verdict.rationale}",
                        "verdict": verdict.model_dump(mode="json"),
                    })
                    if self.tracer is not None:
                        self.tracer.record(step="page_judge", tool="page_judge", status="rejected",
                                           input_count=1, output_count=0,
                                           detail=verdict.model_dump(mode="json"))
                    continue

            confidence = 0.6
            metadata: dict = {"links": p.links[:20], "provider": result_set.provider}
            if verdict is not None:
                if verdict.verdict == "flag":
                    self.flagged += 1
                    confidence = 0.4  # flagged evidence is down-weighted, not trusted
                metadata["gate_status"] = "flagged" if verdict.verdict == "flag" else "accepted"
                metadata["judge"] = verdict.model_dump(mode="json")

            p.evidence_ref = self.ledger.record(
                source_tool=source_tool,
                url=p.url,
                final_url=p.final_url,
                title=p.title,
                snippet=(p.text or "")[:500],
                text=p.text,
                language=p.language,
                confidence=confidence,
                metadata=metadata,
            )
            kept.append(p)
        result_set.results = kept
        if self.wayback is not None and result_set.errors:
            await self._recover_dead_urls(result_set)
        if self.tracer is not None:
            self.tracer.record(step="fetch", tool=source_tool, status="success",
                               input_count=len(urls), output_count=len(result_set.results))
        return result_set

    async def _recover_dead_urls(self, result_set: FetchResultSet) -> None:
        """T-5.2: recover dead/404 fetch errors from the Wayback archive.

        Each recovered snapshot is recorded as a `wayback_cdx` evidence item at a
        reduced confidence (archived content is temporally uncertain) and appended
        to the result set so downstream extraction can still use it.
        """
        for err in result_set.errors:
            dead_url = err.get("url")
            # Skip judge-rejected pages: an err carrying a "verdict" came from the
            # page-judge gate (low-authority / off-topic), not a dead link. Recovering
            # its archived copy would re-persist a page the gate excluded, bypassing
            # the safety gate. Only genuine dead/transport errors are recovered.
            if not dead_url or err.get("recovered_via") or err.get("verdict"):
                continue
            snap = await self.wayback.recover(dead_url)
            if snap is None:
                continue
            ref = self.ledger.record(
                source_tool="wayback_cdx",
                url=snap.original_url,
                final_url=snap.archive_url,
                snippet=(snap.text or "")[:500],
                text=snap.text,
                confidence=0.5,
                metadata={
                    "recovered_from": dead_url,
                    "archive_url": snap.archive_url,
                    "wayback_timestamp": snap.timestamp,
                    "provider": "wayback_cdx",
                },
            )
            result_set.results.append(FetchResult(
                url=snap.original_url, final_url=snap.archive_url, text=snap.text,
                source_tool="wayback_cdx", evidence_ref=ref,
            ))
            err["recovered_via"] = "wayback_cdx"
            self.recovered += 1
            if self.tracer is not None:
                self.tracer.record(step="wayback_recover", tool="wayback_cdx", status="success",
                                   input_count=1, output_count=1,
                                   detail={"recovered_from": dead_url, "archive_url": snap.archive_url})


def build_fetch_provider(name: str | None = None, *, fixtures: dict | None = None) -> object:
    """Construct a fetch provider by name (env SPIDER_QWEN_FETCH_PROVIDER)."""
    name = (name or os.getenv("SPIDER_QWEN_FETCH_PROVIDER", "tinyfish")).lower()
    if name == "mock":
        return MockFetchProvider(fixtures=fixtures)
    if name == "qwen_web_extractor":
        from .qwen_web_extractor import QwenWebExtractorFetchProvider

        return QwenWebExtractorFetchProvider()
    return TinyFishFetchProvider()


_MOCK_MPN_RE = re.compile(r"\b[a-z]+\d[a-z0-9]*\b", re.I)
_OBSOLETE_KEYWORDS = (
    "obsolete", "substitute", "replacement", "eol", "discontinued",
    "cross reference", "cross-reference", "alternate", "nrnd",
)


def _first_mock_mpn(topic: str) -> str:
    """First MPN-shaped token (letters then a digit) in a mock topic, uppercased."""
    for m in _MOCK_MPN_RE.finditer(topic or ""):
        tok = m.group(0)
        if len(tok) >= 4 and any(c.isalpha() for c in tok) and any(c.isdigit() for c in tok):
            return tok.upper()
    return ""


def _looks_like_obsolete_part_topic(topic: str) -> bool:
    low = (topic or "").lower()
    return bool(_first_mock_mpn(low)) and any(k in low for k in _OBSOLETE_KEYWORDS)


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
