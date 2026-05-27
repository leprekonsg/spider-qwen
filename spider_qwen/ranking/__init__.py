"""Mode-specific rankers and SEA-first geo strategy.

Rankers refuse to score evidence-less candidates: every ranked output carries
evidence_refs. SEA-first is a scoring boost, never a hard filter.
"""

from __future__ import annotations

from .geo_strategy import GeoStrategy, build_query_templates
from .product_ranker import ProductRanker
from .service_ranker import ServiceRanker
from .contact_ranker import ContactRanker

__all__ = [
    "GeoStrategy",
    "build_query_templates",
    "ProductRanker",
    "ServiceRanker",
    "ContactRanker",
]
