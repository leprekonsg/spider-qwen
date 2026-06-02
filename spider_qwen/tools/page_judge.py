"""T-2.1: Prometheus-style page judge gate.

Scores each fetched page on a four-dimension rubric -- relevance, freshness,
source-authority, contradicts-ledger -- and returns ``accept | flag | reject``
*before* the ledger writer persists the page. A low-authority or off-topic page
is rejected (never silently stored); a borderline page is flagged and
down-weighted but kept.

The judge is deterministic and offline by default (domain + heuristic scoring,
matching the rest of spider-qwen's hot path). An optional Qwen ``llm`` callable
may override the verdict when keys are present; any failure falls back to the
heuristic. The source-authority tiers here are deliberately light -- T-2.4 folds
them into the policy-driven source-reliability prior + Merkle ledger.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlparse

from pydantic import BaseModel

from ..serendipity.corrective import content_terms

# Named authoritative sources (suffix-matched so subdomains count, not substrings
# -- "multi.com" must not read as the manufacturer "ti.com").
_MANUFACTURERS = (
    "ti.com", "analog.com", "st.com", "nxp.com", "infineon.com", "microchip.com",
    "onsemi.com", "renesas.com", "rohm.com", "vishay.com", "diodes.com",
    "hirose.com", "molex.com", "te.com", "amphenol.com", "samtec.com",
    "murata.com", "tdk.com", "bourns.com",
)
_DISTRIBUTORS = (
    "digikey.com", "digikey.sg", "mouser.com", "mouser.sg", "arrow.com",
    "avnet.com", "farnell.com", "newark.com", "element14.com", "rs-online.com",
    "rsdelivers.com", "future.com", "verical.com",
)
_AGGREGATORS = (
    "octopart.com", "oemsecrets.com", "findchips.com", "alldatasheet.com",
    "datasheets.com", "componentsearchengine.com",
)
# Brokers + marketplaces are keyword-matched: domains vary, and the consequence
# (flag / reject) is conservative either way.
_BROKERS = (
    "rochester", "lansdale", "brokerforum", "netcomponents", "icsource",
)
_MARKETPLACES = (
    "alibaba", "aliexpress", "made-in-china", "ebay", "amazon", "indiamart",
    "tradeindia", "dhgate",
)

_AUTHORITY = {
    "manufacturer": 0.95,
    "distributor": 0.85,
    "government": 0.8,
    "aggregator": 0.6,
    "business": 0.55,
    "broker": 0.45,
    "unknown": 0.4,
    "marketplace": 0.2,
}

_PROCUREMENT_SIGNALS = (
    "quote", "quotation", "rfq", "vendor", "supplier", "price", "pricing",
    "contact", "sales", "catalog", "catalogue", "datasheet", "stock", "moq",
    "pte ltd", "we supply", "we provide", "request a quote",
)
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_PRICE_RE = re.compile(
    r"(?:S\$|US\$|RM|SGD|USD|MYR|EUR|GBP|\$|£|€)\s?\d[\d,]*(?:\.\d+)?",
    re.IGNORECASE,
)


class PageVerdict(BaseModel):
    url: str
    verdict: str  # accept | flag | reject
    score: float
    relevance: float
    freshness: float
    authority: float
    contradiction: float
    source_class: str
    rationale: str = ""


def _host(url: str) -> str:
    host = (urlparse(url).netloc or url or "").lower()
    return host[4:] if host.startswith("www.") else host


def _suffix_match(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def _classify_source(host: str, text: str, title: str) -> str:
    if any(k in host for k in _MARKETPLACES):
        return "marketplace"
    if _suffix_match(host, _DISTRIBUTORS):
        return "distributor"
    if _suffix_match(host, _MANUFACTURERS):
        return "manufacturer"
    if any(k in host for k in _BROKERS):
        return "broker"
    if _suffix_match(host, _AGGREGATORS):
        return "aggregator"
    if host.endswith(".gov") or ".gov." in host or host.endswith(".edu") or ".edu." in host:
        return "government"
    blob = f"{title} {text}".lower()
    return "business" if any(s in blob for s in _PROCUREMENT_SIGNALS) else "unknown"


def _relevance(query: str, title: str, text: str) -> float:
    terms = set(content_terms(query))
    if not terms:
        return 1.0  # nothing topical to match -> do not penalise
    blob = f"{title} {text}".lower()
    matched = sum(1 for t in terms if t in blob)
    return round(matched / len(terms), 4)


def _freshness(text: str, current_year: int) -> float:
    years = [int(y) for y in _YEAR_RE.findall(text or "")]
    years = [y for y in years if 1990 <= y <= current_year + 1]
    if not years:
        return 0.5  # unknown date -> neutral
    newest = min(max(years), current_year)
    age = max(0, current_year - newest)
    return round(max(0.0, min(1.0, 1.0 - age / 10.0)), 4)


def _prices(text: str) -> set[str]:
    return {re.sub(r"\s+", "", m).upper() for m in _PRICE_RE.findall(text or "")}


def _contradiction(text: str, host: str, prior_items: Any) -> float:
    now_prices = _prices(text)
    if not now_prices:
        return 0.0
    for it in prior_items or ():
        it_url = getattr(it, "final_url", None) or getattr(it, "url", "") or ""
        if _host(it_url) != host:
            continue
        prev = _prices(getattr(it, "text", None) or getattr(it, "snippet", "") or "")
        if prev and now_prices.isdisjoint(prev):
            return 0.6  # same source, disjoint prices -> contradicts the ledger
    return 0.0


def _verdict(authority: float, relevance: float, freshness: float,
             contradiction: float, score: float) -> str:
    if authority <= 0.25 or relevance <= 0.1 or contradiction >= 0.8:
        return "reject"
    if authority < 0.5 or freshness <= 0.25 or contradiction >= 0.4 or score < 0.45:
        return "flag"
    return "accept"


class PageJudge:
    """Rubric-scored accept/flag/reject gate for fetched pages."""

    def __init__(
        self,
        *,
        current_year: int | None = None,
        llm: Callable[[str], dict] | None = None,
    ) -> None:
        self.current_year = int(current_year) if current_year else datetime.now(timezone.utc).year
        self.llm = llm

    def judge(
        self,
        *,
        url: str,
        title: str = "",
        text: str = "",
        query: str = "",
        prior_items: Any = (),
        final_url: str | None = None,
    ) -> PageVerdict:
        page_url = final_url or url
        host = _host(page_url)
        source_class = _classify_source(host, text, title or "")
        authority = _AUTHORITY[source_class]
        relevance = _relevance(query, title or "", text)
        freshness = _freshness(text, self.current_year)
        contradiction = _contradiction(text, host, prior_items)
        score = round(
            max(0.0, min(1.0, 0.4 * relevance + 0.35 * authority
                         + 0.15 * freshness - 0.3 * contradiction)),
            4,
        )
        verdict = _verdict(authority, relevance, freshness, contradiction, score)
        result = PageVerdict(
            url=page_url, verdict=verdict, score=score, relevance=relevance,
            freshness=freshness, authority=authority, contradiction=contradiction,
            source_class=source_class,
            rationale=(f"class={source_class} rel={relevance} fresh={freshness} "
                       f"auth={authority} contra={contradiction}"),
        )
        if self.llm is not None:
            result = self._apply_llm(result, query=query, url=page_url, title=title or "", text=text)
        return result

    def _apply_llm(self, base: PageVerdict, **ctx: str) -> PageVerdict:
        try:
            override = self.llm(_judge_prompt(**ctx))  # type: ignore[misc]
        except Exception:
            return base
        if not isinstance(override, dict):
            return base
        data = base.model_dump()
        for key in ("verdict", "relevance", "freshness", "authority",
                    "contradiction", "score", "rationale"):
            if override.get(key) is not None:
                data[key] = override[key]
        return PageVerdict(**data)


def _judge_prompt(*, query: str, url: str, title: str, text: str) -> str:
    return (
        "Judge whether this fetched page is trustworthy procurement evidence for "
        "the buyer query. Score relevance, freshness, source-authority and whether "
        "it contradicts known facts, then return ONLY a JSON object with keys "
        "verdict (accept|flag|reject), relevance, freshness, authority, "
        "contradiction, score, rationale.\n"
        f"Buyer query: {query}\nPage URL: {url}\nTitle: {title}\n\n"
        f"Page text:\n{(text or '')[:8000]}"
    )
