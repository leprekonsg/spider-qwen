"""Budget object, stop reasons, and the per-run budget tracker.

Every mode obeys the stop tuple (max_tool_calls, min_validated_candidates,
evidence_completeness_threshold). The tracker blocks tool calls once a limit is
hit and records the precise StopReason.
"""

from __future__ import annotations

import time
from enum import Enum

from pydantic import BaseModel

from .. import SCHEMA_VERSION


class StopReason(str, Enum):
    MIN_VALIDATED_CANDIDATES_MET = "min_validated_candidates_met"
    MAX_SEARCH_CALLS_REACHED = "max_search_calls_reached"
    MAX_FETCH_URLS_REACHED = "max_fetch_urls_reached"
    MAX_RUNTIME_REACHED = "max_runtime_reached"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    NO_QUOTE_CHANNEL_FOUND = "no_quote_channel_found"
    POLICY_BLOCKED = "policy_blocked"
    ERROR = "error"


class BudgetExceeded(Exception):
    """Raised when a tool call is attempted past its budget limit."""

    def __init__(self, reason: StopReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class Budget(BaseModel):
    schema_version: str = SCHEMA_VERSION
    mode: str
    max_search_calls: int = 4
    max_fetch_urls: int = 20
    max_candidates_to_extract: int = 10
    max_validated_candidates: int = 5
    max_runtime_seconds: int = 180
    min_validated_candidates: int = 3
    evidence_completeness_threshold: float = 0.65


class BudgetTracker:
    """Mutable counters guarded against a Budget; raises BudgetExceeded on breach."""

    def __init__(self, budget: Budget) -> None:
        self.budget = budget
        self.search_calls = 0
        self.fetch_urls = 0
        self.candidates_extracted = 0
        self._started = time.monotonic()
        self.stop_reason: StopReason | None = None

    # --- guards -----------------------------------------------------------
    def can_search(self) -> bool:
        return self.search_calls < self.budget.max_search_calls and not self.runtime_exceeded()

    def remaining_search_calls(self) -> int:
        if self.runtime_exceeded():
            return 0
        return max(0, self.budget.max_search_calls - self.search_calls)

    def can_fetch(self, count: int = 1) -> bool:
        return (
            self.fetch_urls + count <= self.budget.max_fetch_urls
            and not self.runtime_exceeded()
        )

    def runtime_exceeded(self) -> bool:
        return self.elapsed_seconds() >= self.budget.max_runtime_seconds

    def elapsed_seconds(self) -> float:
        return time.monotonic() - self._started

    # --- consumption ------------------------------------------------------
    def consume_search(self) -> None:
        if self.runtime_exceeded():
            self.stop_reason = StopReason.MAX_RUNTIME_REACHED
            raise BudgetExceeded(self.stop_reason, "max runtime reached before search")
        if not self.can_search():
            self.stop_reason = StopReason.MAX_SEARCH_CALLS_REACHED
            raise BudgetExceeded(self.stop_reason, "max search calls reached")
        self.search_calls += 1

    def consume_fetch(self, count: int) -> int:
        """Consume up to `count` fetch URL-units, clamped to the remaining budget.

        Returns how many URLs may actually be fetched (0 if none remain).
        """
        if self.runtime_exceeded():
            self.stop_reason = StopReason.MAX_RUNTIME_REACHED
            raise BudgetExceeded(self.stop_reason, "max runtime reached before fetch")
        remaining = self.budget.max_fetch_urls - self.fetch_urls
        if remaining <= 0:
            self.stop_reason = StopReason.MAX_FETCH_URLS_REACHED
            raise BudgetExceeded(self.stop_reason, "max fetch URLs reached")
        allowed = min(count, remaining)
        self.fetch_urls += allowed
        return allowed

    def consume_extraction(self, count: int = 1) -> bool:
        if self.candidates_extracted >= self.budget.max_candidates_to_extract:
            return False
        self.candidates_extracted += count
        return True

    def snapshot(self) -> dict[str, float | int | str | None]:
        return {
            "search_calls": self.search_calls,
            "fetch_urls": self.fetch_urls,
            "candidates_extracted": self.candidates_extracted,
            "elapsed_seconds": round(self.elapsed_seconds(), 3),
            "stop_reason": self.stop_reason.value if self.stop_reason else None,
            # Caps so callers (e.g. the web UI budget burn-down) can show used/cap.
            "max_search_calls": self.budget.max_search_calls,
            "max_fetch_urls": self.budget.max_fetch_urls,
            "max_candidates_to_extract": self.budget.max_candidates_to_extract,
            "max_validated_candidates": self.budget.max_validated_candidates,
        }
