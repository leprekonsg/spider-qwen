"""Lead frontier: score-before-fetch prioritization of URLs and queries.

The gather phase historically spent its fetch budget in discovery order: the
first query's SERP results crowded out possibly better results from later
queries, and a directory page, a PDF, and a vendor homepage all cost one fetch
with equal priority. The frontier turns every fetchable URL (SERP result,
1-hop page link) and every follow-up query into a ``Lead`` with a
deterministic value score, so the budget is spent on the highest-scoring
leads instead.

Scoring is deterministic and unit-testable: source-reliability prior (shared
governance tiering) + query-term overlap on title/snippet/path + geo TLD
match + a contact/quote path bonus, minus a per-hop depth penalty. An
optional Qwen seam may re-order leads by a clamped delta; it never admits a
lead the deterministic side dropped.
"""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel

from ..evidence.dedupe import canonicalize_url
from ..governance.source_reliability import host_of, reliability_for

# Leads below the floor are never worth a fetch; popping stops there (the
# marginal-value stopping rule, kept as a constant in v1).
SCORE_FLOOR = 0.1
# 1-hop hard cap: links found on fetched pages, never links-of-links. A deeper
# frontier is a crawler, with politeness/trap problems out of v1 scope.
MAX_DEPTH = 1
# How much an external re-scorer (the Qwen seam) may move one lead's score.
MAX_SCORER_DELTA = 0.2
# Drain-loop safety bound; budgets are the real stopping rule.
MAX_ROUNDS = 4

_W_RELIABILITY = 0.5
_W_OVERLAP = 0.3
_GEO_TARGET_BONUS = 0.15
_GEO_SEA_BONUS = 0.08
_CONTACT_PATH_BONUS = 0.1
_DEPTH_PENALTY = 0.15

_SEA_CC_TLDS = frozenset({"sg", "my", "id", "th", "vn", "ph", "bn", "kh", "la", "mm"})
_CONTACT_PATH_RE = re.compile(r"contact|quote|quotation|rfq|enquir|inquir|about", re.IGNORECASE)
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset({"the", "a", "an", "and", "or", "of", "for", "in", "to", "with"})

# A fetched page is treated as a directory (cross-domain links worth following)
# only when it offers at least this many distinct external domains.
DIRECTORY_MIN_DOMAINS = 5
_MAX_LINK_LEADS_PER_PAGE = 10


class Lead(BaseModel):
    kind: Literal["url", "query"]
    value: str
    score: float = 0.0
    depth: int = 0
    provenance: str = "serp"  # serp | entity_serp | template | contact_link | directory_link | entity_query
    title: str | None = None
    snippet: str = ""
    parent_url: str | None = None


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _STOPWORDS}


def term_overlap(query: str, *texts: str) -> float:
    """Fraction of query terms present in the combined texts (0..1)."""
    q = _tokens(query)
    if not q:
        return 0.0
    blob = _tokens(" ".join(t or "" for t in texts))
    return len(q & blob) / len(q)


def score_url_lead(
    url: str,
    *,
    query: str,
    title: str = "",
    snippet: str = "",
    priors: dict[str, float] | None = None,
    target_cc: str | None = None,
    depth: int = 0,
) -> float:
    """Deterministic fetch-worthiness of a URL, clamped to [0, 1]."""
    _, reliability = reliability_for(url, text=snippet, title=title or "", priors=priors)
    path = urlparse(url).path.replace("-", " ").replace("/", " ")
    score = _W_RELIABILITY * reliability + _W_OVERLAP * term_overlap(query, title, snippet, path)
    cc = host_of(url).rsplit(".", 1)[-1]
    if target_cc and cc == target_cc.lower():
        score += _GEO_TARGET_BONUS
    elif cc in _SEA_CC_TLDS:
        score += _GEO_SEA_BONUS
    if _CONTACT_PATH_RE.search(urlparse(url).path or ""):
        score += _CONTACT_PATH_BONUS
    score -= _DEPTH_PENALTY * depth
    return round(max(0.0, min(1.0, score)), 4)


def leads_from_search_results(
    results: list,
    *,
    query: str,
    priors: dict[str, float] | None = None,
    target_cc: str | None = None,
) -> list[Lead]:
    """SERP results -> scored url leads. The snippet/title the SERP already paid
    for is what prices the fetch."""
    leads = []
    for r in results:
        leads.append(Lead(
            kind="url",
            value=r.url,
            score=score_url_lead(
                r.url, query=query, title=r.title or "", snippet=r.snippet,
                priors=priors, target_cc=target_cc,
            ),
            title=r.title,
            snippet=r.snippet,
            provenance="serp",
        ))
    return leads


def rank_leads(leads: list[Lead]) -> list[Lead]:
    """Score-descending; Python's stable sort preserves discovery order on ties,
    so equal-scored leads behave exactly as the pre-frontier pipeline did."""
    return sorted(leads, key=lambda l: -l.score)


def link_leads_from_page(
    page,
    *,
    query: str,
    priors: dict[str, float] | None = None,
    target_cc: str | None = None,
) -> list[Lead]:
    """1-hop leads from a fetched page's outbound links.

    Two patterns earn a follow-up fetch:
    - same-domain contact/quote/about pages (the vendor homepage that links to
      /contact, where the quote channel actually lives);
    - cross-domain links from directory-shaped pages (>= DIRECTORY_MIN_DOMAINS
      distinct external domains), each one a vendor lead.
    """
    page_url = page.final_url or page.url
    base = _registrable(page_url)
    same_domain: list[str] = []
    cross_domain: list[str] = []
    for link in page.links or []:
        if not link.startswith(("http://", "https://")):
            continue
        if _registrable(link) == base:
            if _CONTACT_PATH_RE.search(urlparse(link).path or ""):
                same_domain.append(link)
        else:
            cross_domain.append(link)

    candidates: list[tuple[str, str]] = [(u, "contact_link") for u in same_domain]
    if len({_registrable(u) for u in cross_domain}) >= DIRECTORY_MIN_DOMAINS:
        candidates += [(u, "directory_link") for u in cross_domain]

    leads = []
    for link, provenance in candidates[:_MAX_LINK_LEADS_PER_PAGE]:
        leads.append(Lead(
            kind="url",
            value=link,
            score=score_url_lead(link, query=query, priors=priors, target_cc=target_cc, depth=1),
            depth=1,
            provenance=provenance,
            parent_url=page_url,
        ))
    return leads


def entity_query_lead(vendor_name: str, country: str | None = None) -> Lead:
    """Follow-up query for a vendor we extracted but could not fully ground."""
    text = f'"{vendor_name}" {country or "Singapore"} contact request quotation'
    return Lead(kind="query", value=text, score=0.45, depth=1, provenance="entity_query")


def apply_scorer_deltas(leads: list[Lead], deltas: dict[str, float]) -> int:
    """Apply external (Qwen-proposed) score adjustments, clamped to
    +/- MAX_SCORER_DELTA. Reorders only: a lead that was admitted stays
    admitted, scores stay within [0, 1]. Returns how many leads moved."""
    moved = 0
    for lead in leads:
        delta = deltas.get(lead.value)
        if delta is None:
            continue
        delta = max(-MAX_SCORER_DELTA, min(MAX_SCORER_DELTA, float(delta)))
        adjusted = round(max(0.0, min(1.0, lead.score + delta)), 4)
        if adjusted != lead.score:
            lead.score = adjusted
            moved += 1
    return moved


def _registrable(url: str) -> str:
    host = host_of(url)
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _key(lead: Lead) -> str:
    if lead.kind == "url":
        return "u:" + canonicalize_url(lead.value)
    return "q:" + " ".join((lead.value or "").lower().split())


class Frontier:
    """Priority queue of leads with dedupe and the score floor.

    Sizes are tens of leads, so pops are O(n) scans over a dict -- simpler than
    a heap and trivially deterministic (stable insertion order breaks ties).
    """

    def __init__(self, *, floor: float = SCORE_FLOOR) -> None:
        self.floor = floor
        self._leads: dict[str, Lead] = {}
        self._seen: set[str] = set()
        self.stats = {"added": 0, "dropped_floor": 0, "dropped_depth": 0,
                      "dropped_duplicate": 0, "popped": 0}

    def add(self, lead: Lead) -> bool:
        if lead.depth > MAX_DEPTH:
            self.stats["dropped_depth"] += 1
            return False
        if lead.score < self.floor:
            self.stats["dropped_floor"] += 1
            return False
        key = _key(lead)
        if key in self._seen or key in self._leads:
            self.stats["dropped_duplicate"] += 1
            return False
        self._leads[key] = lead
        self.stats["added"] += 1
        return True

    def mark_seen(self, lead: Lead) -> None:
        """Pre-mark a lead (e.g. an already-fetched URL) so it never re-enters."""
        self._seen.add(_key(lead))
        self._leads.pop(_key(lead), None)

    def pop(self, kind: str, limit: int, provenance: str | None = None) -> list[Lead]:
        """Remove and return up to ``limit`` highest-scoring leads of ``kind``
        (optionally restricted to one ``provenance``)."""
        if limit <= 0:
            return []
        matching = [(k, l) for k, l in self._leads.items()
                    if l.kind == kind and (provenance is None or l.provenance == provenance)]
        matching.sort(key=lambda kl: -kl[1].score)
        taken = matching[:limit]
        for key, _ in taken:
            del self._leads[key]
            self._seen.add(key)
        self.stats["popped"] += len(taken)
        return [l for _, l in taken]

    def pending(self, kind: str | None = None, provenance: str | None = None) -> int:
        if kind is None:
            return len(self._leads)
        return sum(1 for l in self._leads.values()
                   if l.kind == kind and (provenance is None or l.provenance == provenance))

    def url_leads(self) -> list[Lead]:
        """Current url leads (for external re-scoring); order is insertion order."""
        return [l for l in self._leads.values() if l.kind == "url"]
