"""Semantic memory MVP: evidence-backed facts with a conflict policy.

Only evidence-backed facts are stored. Conflict policy: agreeing evidence
reinforces (confidence and spans merge); any cross-source contradiction on the
same (entity, property) becomes a disputed record that retains every side, with
the highest-confidence value as primary. Disputed facts must never be used in RFQ
drafts (enforced by callers).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from .. import SCHEMA_VERSION
from ..evidence.models import DisputedAlternative, EvidenceRef, utc_now_iso
from ..modes.contracts import PrivacyClass
from .promotion import contradicts


class SemanticFact(BaseModel):
    schema_version: str = SCHEMA_VERSION
    fact_id: str = Field(default_factory=lambda: f"fact_{uuid4().hex[:12]}")
    entity_type: Literal["vendor", "category", "part"]
    entity_name: str
    field: str
    value: str
    confidence: float = 0.0
    privacy_class: PrivacyClass = PrivacyClass.BUSINESS_CONTACT
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now_iso)
    last_verified_at: str = Field(default_factory=utc_now_iso)
    status: Literal["active", "stale", "disputed"] = "active"
    # Competing values from contradicting sources; the primary value/refs above
    # are the highest-confidence side, these retain every other side (T-2.3).
    disputed_alternatives: list[DisputedAlternative] = Field(default_factory=list)
    # MemoryBank re-access counter: each corroborating observation grows the
    # fact's decay stability (spaced repetition). See memory/decay.py (T-4.1).
    reinforcement_count: int = 0

    def key(self) -> str:
        return f"{self.entity_type}:{self.entity_name.lower()}:{self.field}"


class MemoryRecall(BaseModel):
    fact: SemanticFact
    decayed_confidence: float
    score: float
    reason: str = ""


class SemanticMemory:
    def __init__(self, state_dir: str | Path | None = None, require_evidence: bool = True) -> None:
        self._state_dir = Path(state_dir) if state_dir else None
        self.require_evidence = require_evidence
        self._facts: dict[str, SemanticFact] = {}
        self._load()

    def _path(self) -> Path | None:
        if not self._state_dir:
            return None
        return self._state_dir / "memory" / "semantic.json"

    def _load(self) -> None:
        path = self._path()
        if path and path.exists():
            for raw in json.loads(path.read_text(encoding="utf-8")):
                fact = SemanticFact.model_validate(raw)
                self._facts[fact.fact_id] = fact

    def upsert(self, fact: SemanticFact) -> SemanticFact:
        """Insert or reconcile a fact under the conflict policy."""
        if self.require_evidence and not fact.evidence_refs:
            raise ValueError("Semantic promotion requires at least one evidence_ref")

        existing = self._by_key(fact.key())
        if existing is None:
            self._facts[fact.fact_id] = fact
            self._persist()
            return fact

        if not contradicts(existing.value, fact.value):
            # Agreement (exact, or after case/spacing/punctuation normalization):
            # a corroborating observation re-accesses the fact, growing its decay
            # stability (MemoryBank spaced repetition, T-4.1).
            existing.confidence = max(existing.confidence, fact.confidence)
            existing.last_verified_at = utc_now_iso()
            existing.reinforcement_count += 1
            if existing.status != "disputed":
                existing.status = "active"
            existing.evidence_refs = self._merge_refs(existing.evidence_refs, fact.evidence_refs)
        else:
            # Any cross-source contradiction is disputed, retaining every side; the
            # highest-confidence side becomes primary. A more confident claim never
            # silently overwrites and discards the weaker span (T-2.3).
            self._record_dispute(existing, fact)
        self._persist()
        return self._facts[existing.fact_id]

    def _record_dispute(self, existing: SemanticFact, incoming: SemanticFact) -> None:
        """Mark a fact disputed while retaining both values and both spans.

        All competing sides are deduped by value; the highest-confidence side
        becomes the primary value/refs, the rest become ``disputed_alternatives``.
        """
        sides: dict[str, DisputedAlternative] = {}
        for alt in [DisputedAlternative(value=existing.value, confidence=existing.confidence,
                                        evidence_refs=existing.evidence_refs),
                    *existing.disputed_alternatives,
                    DisputedAlternative(value=incoming.value, confidence=incoming.confidence,
                                        evidence_refs=incoming.evidence_refs)]:
            prior = sides.get(alt.value)
            if prior is None:
                sides[alt.value] = alt
            else:
                prior.confidence = max(prior.confidence, alt.confidence)
                prior.evidence_refs = self._merge_refs(prior.evidence_refs, alt.evidence_refs)
        ordered = sorted(sides.values(), key=lambda s: s.confidence, reverse=True)
        primary = ordered[0]
        existing.status = "disputed"
        existing.last_verified_at = utc_now_iso()
        existing.value = primary.value
        existing.confidence = primary.confidence
        existing.evidence_refs = primary.evidence_refs
        existing.disputed_alternatives = ordered[1:]

    def get(self, fact_id: str) -> SemanticFact | None:
        return self._facts.get(fact_id)

    def all(self) -> list[SemanticFact]:
        return list(self._facts.values())

    def active(self) -> list[SemanticFact]:
        return [f for f in self._facts.values() if f.status == "active"]

    def maintain(self, *, stale_days: float | None = None) -> int:
        """Apply staleness policy and persist changed facts."""
        from .decay import DEFAULT_STALE_DAYS, is_stale

        threshold = DEFAULT_STALE_DAYS if stale_days is None else stale_days
        changed = 0
        for fact in self._facts.values():
            if fact.status == "active" and is_stale(fact, stale_days=threshold):
                fact.status = "stale"
                changed += 1
        if changed:
            self._persist()
        return changed

    def recall(
        self,
        query: str,
        *,
        top_k: int = 5,
        context_budget_chars: int = 1200,
    ) -> list[MemoryRecall]:
        """Return active facts that fit a simple limited-context budget."""
        from .decay import apply_decay

        query_terms = _terms(query)
        recalls: list[MemoryRecall] = []
        for fact in self.active():
            decayed = apply_decay(fact)
            haystack = _terms(f"{fact.entity_name} {fact.field} {fact.value}")
            overlap = len(query_terms & haystack)
            if overlap <= 0:
                continue
            score = round(decayed * (1.0 + min(overlap, 4) * 0.1), 4)
            recalls.append(
                MemoryRecall(
                    fact=fact,
                    decayed_confidence=round(decayed, 4),
                    score=score,
                    reason=f"{overlap} query term(s) matched",
                )
            )
        recalls.sort(key=lambda r: r.score, reverse=True)
        out: list[MemoryRecall] = []
        used = 0
        for recall in recalls[:top_k]:
            size = len(recall.fact.entity_name) + len(recall.fact.field) + len(recall.fact.value) + 16
            if out and used + size > context_budget_chars:
                break
            if size > context_budget_chars:
                continue
            used += size
            out.append(recall)
        return out

    def _by_key(self, key: str) -> SemanticFact | None:
        for fact in self._facts.values():
            if fact.key() == key:
                return fact
        return None

    @staticmethod
    def _merge_refs(a: list[EvidenceRef], b: list[EvidenceRef]) -> list[EvidenceRef]:
        seen = {r.ledger_id for r in a}
        return a + [r for r in b if r.ledger_id not in seen]

    def _persist(self) -> None:
        path = self._path()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps([f.model_dump() for f in self._facts.values()], indent=2),
            encoding="utf-8",
        )


def _terms(text: str) -> set[str]:
    return {t for t in "".join(c.lower() if c.isalnum() else " " for c in text).split() if len(t) > 2}
