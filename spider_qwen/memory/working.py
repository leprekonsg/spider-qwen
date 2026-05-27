"""Working memory: scratchpad for a single run, discarded after completion."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class WorkingMemory(BaseModel):
    run_id: str
    query: str
    mode: str
    candidate_urls: list[str] = Field(default_factory=list)
    fetched_pages: list[str] = Field(default_factory=list)
    extracted_candidates: list[dict[str, Any]] = Field(default_factory=list)

    def add_urls(self, urls: list[str]) -> None:
        for u in urls:
            if u and u not in self.candidate_urls:
                self.candidate_urls.append(u)

    def add_fetched(self, urls: list[str]) -> None:
        for u in urls:
            if u and u not in self.fetched_pages:
                self.fetched_pages.append(u)
