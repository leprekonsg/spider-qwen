"""T-1.3: Search-o1 condensation + CRAG corrective retrieval evaluator.

``evaluate_retrieval(query, pages)`` classifies the retrieval as
``correct | ambiguous | incorrect`` from a deterministic relevance heuristic
(content-term coverage + a procurement-intent signal), and condenses each page to
the sentences that mention query terms ("reason-in-documents"). On an
``incorrect`` verdict the controller pivots to broaden/broker queries via
``corrective_queries`` rather than answering from off-target pages.

Deterministic and offline; an optional ``llm`` may enrich the condensation but is
never required.
"""

from __future__ import annotations

import re
from typing import Callable

from pydantic import BaseModel, Field

from ..ranking.geo_strategy import SEA_COUNTRIES
from .query_rewrite import SearchQuery, expand_query

# Words that carry no part/service-specific meaning for relevance judging.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "with", "by",
    "is", "are", "be", "this", "that", "from", "as", "at", "we", "you", "our",
    "used", "use", "need", "needed", "looking", "find", "want", "deliver",
    "delivery", "within", "days", "day", "new", "old",
}
_GENERIC = {
    "request", "quotation", "quote", "rfq", "price", "pricing", "prices",
    "supplier", "suppliers", "vendor", "vendors", "contact", "sales", "buy",
    "purchase", "order", "service", "services", "company", "pte", "ltd",
}
_GEO = {c.lower() for c in SEA_COUNTRIES} | {"southeast", "asia", "sea", "global", "region"}

# A page that talks like a procurement source even if it is off-topic.
_PROCUREMENT_SIGNALS = (
    "quote", "quotation", "rfq", "vendor", "supplier", "price", "pricing",
    "contact", "sales", "catalog", "catalogue", "datasheet", "stock", "moq",
)

_SENTENCE_RE = re.compile(r"[^.!?\n]+[.!?]?")


class PageAssessment(BaseModel):
    url: str
    relevance: float
    condensed: str = ""


class CorrectiveVerdict(BaseModel):
    verdict: str  # correct | ambiguous | incorrect
    confidence: float = 0.0
    mean_relevance: float = 0.0
    max_relevance: float = 0.0
    assessments: list[PageAssessment] = Field(default_factory=list)
    rationale: str = ""


def content_terms(query: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9][a-z0-9\-]*", (query or "").lower())
    return [t for t in tokens if len(t) > 2 and t not in _STOPWORDS
            and t not in _GENERIC and t not in _GEO]


def _page_relevance(terms: list[str], text: str, title: str) -> float:
    blob = f"{title} {text}".lower()
    if terms:
        matched = sum(1 for t in set(terms) if t in blob)
        coverage = matched / len(set(terms))
    else:
        coverage = 1.0  # nothing specific to match -> don't penalise
    signal = 1.0 if any(s in blob for s in _PROCUREMENT_SIGNALS) else 0.0
    return round(0.6 * coverage + 0.4 * signal, 4)


def _condense(terms: list[str], text: str, max_sentences: int = 3) -> str:
    sentences = [s.strip() for s in _SENTENCE_RE.findall(text or "") if s.strip()]
    if not terms:
        picked = sentences[:max_sentences]
    else:
        picked = [s for s in sentences if any(t in s.lower() for t in terms)][:max_sentences]
        if not picked:
            picked = sentences[:max_sentences]
    return " ".join(picked)[:500]


def evaluate_retrieval(
    query: str,
    pages: list,
    *,
    correct_threshold: float = 0.5,
    incorrect_threshold: float = 0.1,
    llm: Callable[[str, str], str] | None = None,
) -> CorrectiveVerdict:
    terms = content_terms(query)
    assessments: list[PageAssessment] = []
    for p in pages:
        text = getattr(p, "text", "") or ""
        if not text:
            continue
        rel = _page_relevance(terms, text, getattr(p, "title", "") or "")
        assessments.append(PageAssessment(
            url=getattr(p, "final_url", None) or getattr(p, "url", "") or "",
            relevance=rel,
            condensed=_condense(terms, text),
        ))

    if not assessments:
        return CorrectiveVerdict(verdict="incorrect", confidence=0.0,
                                 mean_relevance=0.0, max_relevance=0.0,
                                 rationale="no pages retrieved")

    mean = round(sum(a.relevance for a in assessments) / len(assessments), 4)
    mx = max(a.relevance for a in assessments)
    if mean >= correct_threshold or mx >= 0.6:
        verdict, confidence = "correct", mx
    elif mean <= incorrect_threshold and mx <= 0.2:
        verdict, confidence = "incorrect", round(1.0 - mx, 4)
    else:
        verdict, confidence = "ambiguous", 0.5
    return CorrectiveVerdict(
        verdict=verdict,
        confidence=round(float(confidence), 4),
        mean_relevance=mean,
        max_relevance=mx,
        assessments=assessments,
        rationale=f"mean={mean} max={mx}",
    )


def corrective_queries(
    query: str,
    verdict: CorrectiveVerdict,
    *,
    mode: str | None = None,
    llm: Callable[[str], str] | None = None,
) -> list[SearchQuery]:
    """Broaden/broker-pivot queries for an off-target retrieval (broker first)."""
    variants = expand_query(query, mode=mode, llm=llm)
    order = {"broker_operator": 0, "step_back": 1, "obsolescence": 2}
    picks = [v for v in variants if v.kind in order]
    return sorted(picks, key=lambda v: order[v.kind])
