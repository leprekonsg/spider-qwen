"""Mode router: maps a mode to its extraction pipeline, ranker, and outputs.

Kept declarative so the controller stays a thin orchestrator and the per-mode
behavior is inspectable in one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .contracts import ProcurementMode


@dataclass(frozen=True)
class RoutePlan:
    mode: ProcurementMode
    budget_key: str
    extractors: tuple[str, ...]
    ranker: str
    produces_rfq: bool = False
    # Candidate fields that must be evidence-backed to count toward completeness.
    required_evidence_fields: tuple[str, ...] = field(default_factory=tuple)
    # T-3.3: modes that invoke GRAM-lite Serendipity Mode (width-first discovery).
    # Substitute discovery rides the product serendipity slots; revalidation
    # re-derives known facts -- both benefit from width-first trajectories.
    serendipity_mode: bool = False


_ROUTES: dict[ProcurementMode, RoutePlan] = {
    ProcurementMode.PRODUCT_EXACT_PRICE: RoutePlan(
        mode=ProcurementMode.PRODUCT_EXACT_PRICE,
        budget_key="product_exact_price",
        extractors=("vendor_metadata", "pricing", "contact"),
        ranker="product",
        produces_rfq=False,
        required_evidence_fields=("vendor_name", "pricing_status", "product_url"),
        serendipity_mode=True,  # product substitute discovery
    ),
    ProcurementMode.SERVICE_QUOTE_REQUIRED: RoutePlan(
        mode=ProcurementMode.SERVICE_QUOTE_REQUIRED,
        budget_key="service_quote_required",
        extractors=("vendor_metadata", "service_match", "quote_channel", "contact", "pricing"),
        ranker="service",
        produces_rfq=True,
        required_evidence_fields=("vendor_name", "service_match", "quote_channel"),
    ),
    ProcurementMode.CONTACT_ENRICHMENT_ONLY: RoutePlan(
        mode=ProcurementMode.CONTACT_ENRICHMENT_ONLY,
        budget_key="contact_enrichment_only",
        extractors=("vendor_metadata", "contact"),
        ranker="contact",
        produces_rfq=False,
        required_evidence_fields=("vendor_name", "contact"),
    ),
    # Revalidation reuses the contact/quote extraction path against a known target.
    ProcurementMode.REVALIDATION: RoutePlan(
        mode=ProcurementMode.REVALIDATION,
        budget_key="contact_enrichment_only",
        extractors=("vendor_metadata", "contact", "quote_channel"),
        ranker="contact",
        produces_rfq=False,
        required_evidence_fields=("vendor_name",),
        serendipity_mode=True,  # revalidation re-derives known facts width-first
    ),
}


class ModeRouter:
    def route(self, mode: ProcurementMode) -> RoutePlan:
        return _ROUTES[mode]
