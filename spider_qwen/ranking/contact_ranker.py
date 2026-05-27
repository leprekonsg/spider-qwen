"""Contact ranker (contact_enrichment_only).

Not weighted in the spec; this scoring favors business-contact reliability and
cross-source validation while keeping evidence_completeness material:
  contact_quality 35, validation_signals 25, geo_relevance 15,
  evidence_completeness 25.
"""

from __future__ import annotations

from ..modes.contracts import ContactCandidate, PrivacyClass


class ContactRanker:
    def score(self, c: ContactCandidate) -> float:
        if not c.has_evidence() or not c.contacts:
            return 0.0
        best = max(c.contacts, key=lambda ct: ct.confidence)
        contact_quality = best.confidence * 35
        if best.privacy_class == PrivacyClass.BUSINESS_CONTACT:
            contact_quality = min(35.0, contact_quality + 3)

        signals = c.validation_signals or {}
        cross = min(int(signals.get("cross_source_count", 1)), 3) / 3.0
        domain_match = 1.0 if signals.get("domain_match") else 0.0
        validation = (0.6 * cross + 0.4 * domain_match) * 25

        geo = max(0.0, c.geo_score) / 20.0 * 15
        evidence = c.evidence_completeness * 25
        return round(contact_quality + validation + geo + evidence, 2)

    def rank(self, candidates: list[ContactCandidate]) -> list[ContactCandidate]:
        scored = [c for c in candidates if c.has_evidence() and c.contacts]
        for c in scored:
            c.score = self.score(c)
        return sorted(scored, key=lambda c: c.score, reverse=True)
