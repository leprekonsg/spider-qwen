"""Provider-neutral result types and protocols.

TinyFish Search returns ranked URL results; TinyFish Fetch returns clean
extracted page content for multiple URLs. These types model both without
leaking provider specifics into the extraction or ranking layers.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from ..evidence.models import EvidenceRef


class SearchResult(BaseModel):
    url: str
    title: str | None = None
    snippet: str = ""
    rank: int = 0
    source_tool: str = "search"
    # Populated by SearchService after the result is recorded to the ledger.
    evidence_ref: EvidenceRef | None = None


class SearchResultSet(BaseModel):
    query: str
    location: str | None = None
    results: list[SearchResult] = Field(default_factory=list)
    total_results: int = 0
    provider: str = "search"

    def urls(self) -> list[str]:
        return [r.url for r in self.results]


class FetchResult(BaseModel):
    url: str
    final_url: str | None = None
    title: str | None = None
    text: str = ""
    links: list[str] = Field(default_factory=list)
    language: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    source_tool: str = "fetch"
    # Populated by FetchService after the page is recorded to the ledger.
    evidence_ref: EvidenceRef | None = None


class FetchResultSet(BaseModel):
    results: list[FetchResult] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    provider: str = "fetch"


class ExtractedPage(BaseModel):
    """Single-page extraction result (Qwen WebExtractor fallback shape)."""

    url: str
    final_url: str | None = None
    title: str | None = None
    text: str = ""
    links: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    source_tool: str = "qwen_web_extractor"


@runtime_checkable
class SearchProvider(Protocol):
    async def search(
        self, query: str, location: str | None, language: str, limit: int
    ) -> SearchResultSet: ...


@runtime_checkable
class FetchProvider(Protocol):
    async def fetch(
        self, urls: list[str], output_format: str, include_links: bool
    ) -> FetchResultSet: ...
