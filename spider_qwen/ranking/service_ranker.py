"""Service ranker (service_quote_required).

Scores keep three independent decision dimensions:
  suitability (service match, geography, checklist) 55,
  contactability (the quote channel) 25,
  evidence quality 20, conflict_penalty -20 max.

Quote-channel quality is deliberately counted only in contactability.  A readily
available email can make an otherwise suitable supplier easier to approach; it
cannot also make that supplier a better service match.
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
    def components(self, c: ServiceCandidate) -> dict[str, float]:
        """Return independently interpretable score dimensions.

        Keeping these separate makes qualification reviewable and prevents a
        contact-channel signal from being reused as a technical-fit signal.
        """
        if not c.has_evidence():
            return {"suitability": 0.0, "contactability": 0.0, "evidence_quality": 0.0}
        suitability = (
            min(1.0, max(0.0, c.service_match_score)) * 25
            + min(1.0, max(0.0, c.geo_score) / 20.0) * 20
            + min(1.0, max(0.0, c.checklist_completeness)) * 10
        )
        contactability = (
            _CHANNEL_QUALITY.get(c.quote_channel.type, 0.0) * 25
            if c.quote_channel else 0.0
        )
        evidence_quality = min(1.0, max(0.0, c.evidence_completeness)) * 20
        return {
            "suitability": round(suitability, 2),
            "contactability": round(contactability, 2),
            "evidence_quality": round(evidence_quality, 2),
        }

    def score(self, c: ServiceCandidate) -> float:
        if not c.has_evidence():
            return 0.0
        components = self.components(c)
        penalty = max(-20.0, c.conflict_penalty)
        total = round(sum(components.values()) + penalty, 2)
        c.score_components = {
            **components,
            "conflict_penalty": round(penalty, 2),
            "total": total,
        }
        return total

    def rank(self, candidates: list[ServiceCandidate]) -> list[ServiceCandidate]:
        scored = [c for c in candidates if c.has_evidence()]
        for c in scored:
            c.score = self.score(c)
        # Once a candidate is eligible, technical suitability is the first sort
        # key.  Contact convenience breaks ties; it must not outrank fit.
        return sorted(
            scored,
            key=lambda c: (self.components(c)["suitability"], c.score),
            reverse=True,
        )
