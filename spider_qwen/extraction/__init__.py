"""Deterministic page extractors (regex/heuristic, no LLM).

Each extractor takes already-fetched page text/links and returns structured,
evidence-attachable facts. Missing price is never a global failure: it becomes
PricingStatus.NOT_FOUND / CONTACT_FOR_PRICING.
"""

from __future__ import annotations

from .pricing import PricingExtractor, PricingResult
from .quote_channel import QuoteChannelExtractor, QuoteChannelMatch
from .contact import ContactExtractor, ContactMatch
from .vendor_metadata import VendorMetadataExtractor, VendorMetadata
from .service_match import ServiceMatchExtractor, ServiceMatchResult
from .dedupe import dedupe_candidates

__all__ = [
    "PricingExtractor",
    "PricingResult",
    "QuoteChannelExtractor",
    "QuoteChannelMatch",
    "ContactExtractor",
    "ContactMatch",
    "VendorMetadataExtractor",
    "VendorMetadata",
    "ServiceMatchExtractor",
    "ServiceMatchResult",
    "dedupe_candidates",
]
