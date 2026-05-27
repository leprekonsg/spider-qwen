"""Service match: does the page actually offer the requested service?

Token-overlap heuristic between the user's service terms and the page text.
Produces service_match_evidence (bool) and a 0..1 score the service ranker uses.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

_STOPWORDS = {
    "find", "for", "the", "a", "an", "in", "and", "with", "of", "to", "near",
    "me", "service", "services", "vendor", "vendors", "supplier", "suppliers",
    "company", "companies", "best", "top", "looking", "need", "want", "get",
}


def _terms(text: str) -> list[str]:
    tokens = re.findall(r"[a-z]{3,}", (text or "").lower())
    return [t for t in tokens if t not in _STOPWORDS]


class ServiceMatchResult(BaseModel):
    matched: bool
    score: float
    matched_terms: list[str] = []


class ServiceMatchExtractor:
    def extract(self, query: str, text: str) -> ServiceMatchResult:
        query_terms = set(_terms(query))
        if not query_terms:
            return ServiceMatchResult(matched=False, score=0.0)
        page_terms = set(_terms(text))
        hits = sorted(query_terms & page_terms)
        score = len(hits) / len(query_terms)
        return ServiceMatchResult(matched=score >= 0.34, score=round(score, 3), matched_terms=hits)
