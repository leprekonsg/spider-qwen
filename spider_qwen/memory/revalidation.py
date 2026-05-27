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

    def refresh(
        self,
        fact: SemanticFact,
        *,
        new_value: str | None,
        new_confidence: float,
        new_refs: list[EvidenceRef],
    ) -> SemanticFact:
        if new_value is None or not new_refs:
            fact.status = "stale"
            self.memory._persist()
            return fact

        if new_value == fact.value:
            fact.confidence = max(fact.confidence, new_confidence)
            fact.last_verified_at = utc_now_iso()
            fact.status = "active"
            fact.evidence_refs = SemanticMemory._merge_refs(fact.evidence_refs, new_refs)
        elif new_confidence > fact.confidence + 0.1:
            fact.value = new_value
            fact.confidence = new_confidence
            fact.evidence_refs = new_refs
            fact.last_verified_at = utc_now_iso()
            fact.status = "active"
        else:
            fact.status = "disputed"
        self.memory._persist()
        return fact
