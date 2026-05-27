"""Product ranker (product_exact_price).

Weights (spec section 5.1):
  price_quality 30, MOQ availability 15, vendor/contact completeness 20,
  geo_relevance 15, evidence_completeness 20.
"""

from __future__ import annotations

from ..modes.contracts import PricingStatus, ProductCandidate

_PRICE_QUALITY = {
    PricingStatus.EXACT_PRICE: 1.0,
    PricingStatus.PRICE_RANGE: 0.8,
    PricingStatus.STARTING_FROM: 0.6,
    PricingStatus.RATE_CARD_FOUND: 0.5,
    PricingStatus.CONTACT_FOR_PRICING: 0.2,
    PricingStatus.QUOTE_REQUIRED: 0.2,
    PricingStatus.CONFLICTING: 0.1,
    PricingStatus.NOT_FOUND: 0.0,
}


class ProductRanker:
    def score(self, c: ProductCandidate) -> float:
        if not c.has_evidence():
            return 0.0
        price = _PRICE_QUALITY.get(c.pricing_status, 0.0) * 30
        moq = 15 if c.moq else 0
        completeness = 20 if (c.website and c.evidence_refs) else (10 if c.website else 0)
        geo = max(0.0, c.geo_score) / 20.0 * 15
        evidence = c.evidence_completeness * 20
        return round(price + moq + completeness + geo + evidence, 2)

    def rank(self, candidates: list[ProductCandidate]) -> list[ProductCandidate]:
        scored = [c for c in candidates if c.has_evidence()]
        for c in scored:
            c.score = self.score(c)
        return sorted(scored, key=lambda c: c.score, reverse=True)
