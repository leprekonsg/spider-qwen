"""Manual revalidation (v1).

Refreshes a single semantic fact against newly gathered evidence. Automated
scheduling is deferred to a later phase. If fresh evidence contradicts the
stored value, the fact is marked disputed rather than silently overwritten.
"""

from __future__ import annotations

from ..evidence.models import EvidenceRef, utc_now_iso
from .semantic import SemanticFact, SemanticMemory


class Revalidator:
    def __init__(self, memory: SemanticMemory) -> None:
        self.memory = memory

    def revalidate(
        self,
        *,
        fact_id: str,
        observed_value: str | None,
        evidence_refs: list[EvidenceRef],
        new_confidence: float,
    ) -> SemanticFact | None:
        fact = self.memory.get(fact_id)
        if fact is None:
            return None
        return self.refresh(
            fact,
            new_value=observed_value,
            new_confidence=new_confidence,
            new_refs=evidence_refs,
        )

    def refresh(
        self,
        fact: SemanticFact,
        *,
        new_value: str | None,
        new_confidence: float,
        new_refs: list[EvidenceRef],
    ) -> SemanticFact:
        def mutation(current: SemanticFact) -> None:
            if new_value is None or not new_refs:
                current.status = "stale"
            elif new_value == current.value:
                current.confidence = max(current.confidence, new_confidence)
                current.last_verified_at = utc_now_iso()
                current.status = "active"
                current.evidence_refs = SemanticMemory._merge_refs(current.evidence_refs, new_refs)
            elif new_confidence > current.confidence + 0.1:
                current.value = new_value
                current.confidence = new_confidence
                current.evidence_refs = new_refs
                current.last_verified_at = utc_now_iso()
                current.status = "active"
            else:
                current.status = "disputed"

        refreshed = self.memory._mutate_fact(fact.fact_id, mutation)
        if refreshed is None:
            raise ValueError(f"Semantic fact {fact.fact_id!r} no longer exists")
        return refreshed
