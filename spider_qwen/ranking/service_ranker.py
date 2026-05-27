"""Service ranker (service_quote_required).

Weights (spec section 5.2):
  service_match_quality 25, quote_channel_quality 25, geo_relevance 20,
  contact_reliability 15, checklist_completeness 10, conflict_penalty -20 max.
"""

from __future__ import annotations

from ..modes.contracts import QuoteChannelType, ServiceCandidate

# Quote-channel quality on a 0..1 scale (RFQ form / email are strongest).
_CHANNEL_QUALITY = {
    QuoteChannelType.RFQ_FORM: 1.0,
    QuoteChannelType.CONTACT_EMAIL: 0.9,
    QuoteChannelType.CONTACT_PAGE: 0.6,
    QuoteChannelType.PHONE: 0.5,
    QuoteChannelType.RATE_CARD: 0.4,
    QuoteChannelType.PORTAL_LOGIN_REQUIRED: 0.2,
}


class ServiceRanker:
    def score(self, c: ServiceCandidate) -> float:
        if not c.has_evidence():
            return 0.0
        service_match = min(1.0, c.service_match_score) * 25
        channel = (
            _CHANNEL_QUALITY.get(c.quote_channel.type, 0.0) * 25 if c.quote_channel else 0.0
        )
        geo = max(0.0, c.geo_score) / 20.0 * 20
        # Contact reliability: an email quote channel is the most reliable signal.
        contact = 15 if (c.quote_channel and c.quote_channel.type == QuoteChannelType.CONTACT_EMAIL) else (
            10 if c.quote_channel else 0
        )
        checklist = min(1.0, c.checklist_completeness) * 10
        penalty = max(-20.0, c.conflict_penalty)
        return round(service_match + channel + geo + contact + checklist + penalty, 2)

    def rank(self, candidates: list[ServiceCandidate]) -> list[ServiceCandidate]:
        scored = [c for c in candidates if c.has_evidence()]
        for c in scored:
            c.score = self.score(c)
        return sorted(scored, key=lambda c: c.score, reverse=True)
