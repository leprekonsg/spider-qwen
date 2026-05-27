"""Run metrics (spec section 13)."""

from __future__ import annotations

from pydantic import BaseModel


class Metrics(BaseModel):
    search_calls_total: int = 0
    fetch_urls_total: int = 0
    validated_candidates_total: int = 0
    rfq_drafts_total: int = 0
    rfq_incomplete_total: int = 0
    quote_channel_found: int = 0
    candidates_considered: int = 0
    contact_precision_estimate: float = 0.0
    avg_runtime_seconds: float = 0.0
    budget_exhausted: bool = False

    @property
    def quote_channel_found_rate(self) -> float:
        if self.candidates_considered == 0:
            return 0.0
        return round(self.quote_channel_found / self.candidates_considered, 3)
