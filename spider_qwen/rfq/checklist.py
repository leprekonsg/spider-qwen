"""RFQ required-inputs checklist and completeness scoring.

Completeness measures how grounded the draft is, tied directly to the service
success contract. It is a weighted blend of the minimum viable RFQ signals:
an evidenced quote channel, a service match, vendor identity, and known geo.
Below the policy threshold (default 0.65) the draft is returned `incomplete`.
"""

from __future__ import annotations

from ..evidence.models import EvidenceRef
from ..modes.contracts import ServiceCandidate
from .schema import ChecklistItem

# Buyer-facing inputs a vendor needs to quote a service. These are guidance for
# the buyer to fill in; we attach evidence_ref only where we derived a value.
_SERVICE_INPUTS: tuple[tuple[str, str], ...] = (
    ("service_scope", "Exact scope of work the vendor must price."),
    ("service_location", "Site address / coverage area for the service."),
    ("frequency_or_schedule", "How often / when the service is required."),
    ("site_size_or_headcount", "Area (sqft/sqm) or headcount that drives pricing."),
    ("preferred_start_date", "When the engagement should begin."),
    ("contract_duration", "One-off vs ongoing contract term."),
    ("special_requirements", "Compliance, certifications, or special conditions."),
)

# Weights for completeness (sum to 1.0).
_W_QUOTE_CHANNEL = 0.40
_W_SERVICE_MATCH = 0.25
_W_VENDOR_IDENTITY = 0.20
_W_GEO = 0.15


def compute_checklist_completeness(candidate: ServiceCandidate, target_country: str | None) -> float:
    score = 0.0
    if candidate.quote_channel is not None:
        score += _W_QUOTE_CHANNEL
    if candidate.service_match_evidence:
        score += _W_SERVICE_MATCH
    if candidate.vendor_name and candidate.website:
        score += _W_VENDOR_IDENTITY
    if candidate.country or target_country:
        score += _W_GEO
    return round(score, 3)


class RFQChecklistBuilder:
    def build(
        self,
        *,
        query: str,
        candidate: ServiceCandidate,
        target_country: str | None,
    ) -> list[ChecklistItem]:
        items: list[ChecklistItem] = []
        for field, reason in _SERVICE_INPUTS:
            ref: EvidenceRef | None = None
            # Attach evidence where we can ground the input from extraction.
            if field == "service_location" and (candidate.country or target_country):
                ref = candidate.evidence_refs[0] if candidate.evidence_refs else None
            if field == "service_scope" and candidate.service_match_evidence:
                ref = candidate.evidence_refs[0] if candidate.evidence_refs else None
            items.append(ChecklistItem(field=field, reason=reason, required=True, evidence_ref=ref))
        return items
