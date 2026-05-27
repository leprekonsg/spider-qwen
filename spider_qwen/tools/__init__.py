"""Tool layer: TinyFish Search/Fetch plus optional Qwen providers.

No TinyFish Agent, Browser, code interpreter, or submit/send tools. Providers
are swappable through the SearchProvider / FetchProvider protocols.
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
from .qwen_json_extractor import QwenJsonExtractor, MockQwenJsonExtractor

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
    "QwenJsonExtractor",
    "MockQwenJsonExtractor",
]
