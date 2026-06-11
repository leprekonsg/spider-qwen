"""SAFE-style search-grounded re-verification for flagged atoms.

A claim the MiniCheck gatekeeper could not ground in its own cited span gets a
second chance against a wider corpus: the rest of the run's fetched evidence,
plus (optionally) fresh search results -- TinyFish in place of SAFE's Google. The
claim is upheld only if some independent source grounds the full vendor-scoped
predicate (subject and value in the same span); a coincidental price on another
vendor's page does not count.
"""

from __future__ import annotations

import logging
from typing import Callable

from .atomic import AtomicClaim
from .minicheck import MiniCheck, MiniCheckResult

logger = logging.getLogger(__name__)


class SafeReverifier:
    def __init__(self, minicheck: MiniCheck, *,
                 search_fn: Callable[[str], list[str]] | None = None) -> None:
        self.minicheck = minicheck
        self.search_fn = search_fn

    def reverify(self, claim: AtomicClaim, *, corpus: list[str]) -> MiniCheckResult:
        spans = [s for s in (corpus or []) if s and s.strip()]
        if self.search_fn is not None:
            try:
                spans = spans + [s for s in (self.search_fn(self._query(claim)) or []) if s and s.strip()]
            except Exception as exc:
                logger.warning("SAFE search re-verification failed: %s", exc)

        best = MiniCheckResult(supported=False, score=0.0, method="safe_no_grounding",
                               rationale="no independent source grounds the claim")
        for span in spans:
            result = self.minicheck.check(
                claim=claim.predicate, value=claim.object_value, evidence_span=span,
                field=claim.field, subject=claim.subject,
            )
            if result.score > best.score:
                best = result.model_copy(update={"method": "safe_" + result.method})
                if best.supported and best.score >= 1.0:
                    break
        return best

    @staticmethod
    def _query(claim: AtomicClaim) -> str:
        parts = (claim.subject, claim.field.replace("_", " "), claim.object_value)
        return " ".join(p for p in parts if p).strip()
