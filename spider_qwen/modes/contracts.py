"""Mode enums, success contracts, and candidate schemas.

Every candidate carries evidence_refs. No vendor/pricing/contact/RFQ output is
valid without at least one EvidenceRef backing it (enforced by the controller
and by ranking, which refuses to score evidence-less candidates).
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from .. import SCHEMA_VERSION
from ..evidence.models import EvidenceRef


class ProcurementMode(str, Enum):
    PRODUCT_EXACT_PRICE = "product_exact_price"
    SERVICE_QUOTE_REQUIRED = "service_quote_required"
    CONTACT_ENRICHMENT_ONLY = "contact_enrichment_only"
    REVALIDATION = "revalidation"


class PricingStatus(str, Enum):
    EXACT_PRICE = "EXACT_PRICE"
    PRICE_RANGE = "PRICE_RANGE"
    STARTING_FROM = "STARTING_FROM"
    RATE_CARD_FOUND = "RATE_CARD_FOUND"
    QUOTE_REQUIRED = "QUOTE_REQUIRED"
    CONTACT_FOR_PRICING = "CONTACT_FOR_PRICING"
    NOT_FOUND = "NOT_FOUND"
    CONFLICTING = "CONFLICTING"


class QuoteChannelType(str, Enum):
    RFQ_FORM = "rfq_form"
    CONTACT_EMAIL = "contact_email"
    PHONE = "phone"
    CONTACT_PAGE = "contact_page"
    RATE_CARD = "rate_card"
    PORTAL_LOGIN_REQUIRED = "portal_login_required"


class PrivacyClass(str, Enum):
    BUSINESS_CONTACT = "business_contact"
    NAMED_PERSON_HIGH_SENSITIVITY = "named_person_high_sensitivity"


# Success contracts mirror the plan. They are descriptive (used in docs, traces,
# and benchmarks) and consumed by the controller's stop logic.
SUCCESS_CONTRACTS: dict[ProcurementMode, dict[str, Any]] = {
    ProcurementMode.PRODUCT_EXACT_PRICE: {
        "mode": "product_exact_price",
        "success_if": [
            "at_least_min_validated_candidates",
            "each_candidate_has_numeric_price_or_precise_price_proxy",
            "each_price_has_evidence_ref",
        ],
    },
    ProcurementMode.SERVICE_QUOTE_REQUIRED: {
        "mode": "service_quote_required",
        "success_if": [
            "at_least_one_valid_quote_channel",
            "service_match_evidence_exists",
            "required_inputs_checklist_completeness_above_threshold",
            "rfq_draft_created",
        ],
        "v1_boundary": "RFQ draft only; no portal completion or vendor submission",
    },
    ProcurementMode.CONTACT_ENRICHMENT_ONLY: {
        "mode": "contact_enrichment_only",
        "success_if": [
            "at_least_min_validated_candidates",
            "each_contact_has_evidence_ref",
        ],
    },
    ProcurementMode.REVALIDATION: {
        "mode": "revalidation",
        "success_if": ["target_fact_refreshed_or_marked_stale"],
    },
}


class QuoteChannel(BaseModel):
    """How a buyer requests a quote from a vendor. Always evidence-backed."""

    type: QuoteChannelType
    value: str
    evidence_ref: EvidenceRef


class Contact(BaseModel):
    type: str  # email | phone | contact_form | contact_page
    value: str
    confidence: float = 0.0
    privacy_class: PrivacyClass = PrivacyClass.BUSINESS_CONTACT
    evidence_ref: EvidenceRef


class _BaseCandidate(BaseModel):
    schema_version: str = SCHEMA_VERSION
    vendor_name: str
    website: str | None = None
    country: str | None = None
    geo_score: float = 0.0
    evidence_completeness: float = 0.0
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)

    def has_evidence(self) -> bool:
        return len(self.evidence_refs) > 0


class ProductCandidate(_BaseCandidate):
    product_name: str = ""
    price: float | None = None
    currency: str | None = None
    unit: str | None = None
    moq: str | None = None
    pricing_status: PricingStatus = PricingStatus.NOT_FOUND
    product_url: str = ""
    score: float = 0.0


class ServiceCandidate(_BaseCandidate):
    service_match_score: float = 0.0
    service_match_evidence: bool = False
    pricing_status: PricingStatus = PricingStatus.QUOTE_REQUIRED
    quote_channel: QuoteChannel | None = None
    checklist_completeness: float = 0.0
    conflict_penalty: float = 0.0
    score: float = 0.0


class ContactCandidate(_BaseCandidate):
    contacts: list[Contact] = Field(default_factory=list)
    validation_signals: dict[str, Any] = Field(default_factory=dict)
    score: float = 0.0
