"""Web tool layer: TinyFish Search/Fetch (primary) + Qwen WebExtractor (fallback).

v1 uses only search + fetch. No TinyFish Agent or Browser. Providers are
swappable through the SearchProvider / FetchProvider protocols.
"""

from __future__ import annotations

from .provider_types import (
    ExtractedPage,
    FetchProvider,
    FetchResult,
    FetchResultSet,
    SearchProvider,
    SearchResult,
    SearchResultSet,
)
from .search_service import SearchService, build_search_provider
from .fetch_service import FetchService, build_fetch_provider

__all__ = [
    "SearchResult",
    "SearchResultSet",
    "FetchResult",
    "FetchResultSet",
    "ExtractedPage",
    "SearchProvider",
    "FetchProvider",
    "SearchService",
    "FetchService",
    "build_search_provider",
    "build_fetch_provider",
]
