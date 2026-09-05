"""Evidence-backed semantic facts with identity-safe, atomic persistence."""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Literal, TypeVar
from uuid import uuid4

from pydantic import BaseModel, Field

from .. import SCHEMA_VERSION
from ..evidence.models import DisputedAlternative, EvidenceRef, utc_now_iso
from ..identity import stable_supplier_id
from ..modes.contracts import PrivacyClass
from .promotion import contradicts

_T = TypeVar("_T")
_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    """Hold a process and thread lock for one semantic-memory file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve())
    with _LOCKS_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(key, threading.RLock())
    with thread_lock:
        with path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class SemanticFact(BaseModel):
    schema_version: str = SCHEMA_VERSION
    fact_id: str = Field(default_factory=lambda: f"fact_{uuid4().hex[:12]}")
    entity_type: Literal["vendor", "category", "part"]
    entity_name: str
    supplier_id: str | None = None
    field: str
    value: str
    confidence: float = 0.0
    privacy_class: PrivacyClass = PrivacyClass.BUSINESS_CONTACT
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now_iso)
    last_verified_at: str = Field(default_factory=utc_now_iso)
    status: Literal["active", "stale", "disputed"] = "active"
    disputed_alternatives: list[DisputedAlternative] = Field(default_factory=list)
    reinforcement_count: int = 0
    citation_count: int = 0

    def model_post_init(self, __context: object) -> None:
        if self.entity_type == "vendor" and not self.supplier_id:
            object.__setattr__(self, "supplier_id", stable_supplier_id(self.entity_name))

    def key(self) -> str:
        if self.entity_type == "vendor" and self.supplier_id:
            return f"vendor:{self.supplier_id}:{self.field}"
        return f"{self.entity_type}:{self.entity_name.casefold()}:{self.field}"


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
        return self._state_dir / "memory" / "semantic.json" if self._state_dir else None

    def _lock_path(self) -> Path | None:
        path = self._path()
        return path.with_suffix(".lock") if path else None

    def _read_unlocked(self) -> tuple[dict[str, SemanticFact], bool]:
        path = self._path()
        if path is None or not path.exists():
            return {}, False
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Semantic memory file {path} is unreadable: {exc}") from exc
        if not isinstance(rows, list):
            raise ValueError(f"Semantic memory file {path} must contain a JSON list")
        facts: dict[str, SemanticFact] = {}
        migrated = False
        for raw in rows:
            version = raw.get("schema_version") if isinstance(raw, dict) else None
            if version != SCHEMA_VERSION:
                raise ValueError(
                    f"Semantic memory file {path} contains schema_version {version!r}; "
                    f"expected {SCHEMA_VERSION!r}. Run a memory migration or rebuild "
                    "semantic memory from the evidence ledger."
                )
            fact = SemanticFact.model_validate(raw)
            migrated = migrated or (
                fact.entity_type == "vendor" and not raw.get("supplier_id")
            )
            facts[fact.fact_id] = fact
        return facts, migrated

    def _write_unlocked(self, facts: dict[str, SemanticFact]) -> None:
        path = self._path()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
        payload = json.dumps(
            [fact.model_dump(mode="json") for fact in facts.values()],
            indent=2,
            ensure_ascii=False,
        )
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            for attempt in range(8):
                try:
                    os.replace(temporary, path)
                    break
                except PermissionError:
                    if attempt == 7:
                        raise
                    time.sleep(0.01 * (attempt + 1))
        finally:
            if temporary.exists():
                temporary.unlink()

    def _load(self) -> None:
        lock_path = self._lock_path()
        if lock_path is None:
            return
        with _exclusive_file_lock(lock_path):
            self._facts, migrated = self._read_unlocked()
            if migrated:
                self._write_unlocked(self._facts)

    def _refresh(self) -> None:
        lock_path = self._lock_path()
        if lock_path is None:
            return
        with _exclusive_file_lock(lock_path):
            self._facts, _ = self._read_unlocked()

    def _transaction(self, mutation: Callable[[dict[str, SemanticFact]], _T]) -> _T:
        lock_path = self._lock_path()
        if lock_path is None:
            result = mutation(self._facts)
            return result
        with _exclusive_file_lock(lock_path):
            self._facts, _ = self._read_unlocked()
            result = mutation(self._facts)
            self._write_unlocked(self._facts)
            return result

    def upsert(self, fact: SemanticFact) -> SemanticFact:
        """Atomically insert or reconcile a fact under the conflict policy."""
        if self.require_evidence and not fact.evidence_refs:
            raise ValueError("Semantic promotion requires at least one evidence_ref")

        incoming = fact.model_copy(deep=True)

        def mutation(facts: dict[str, SemanticFact]) -> SemanticFact:
            existing = self._by_key(incoming.key(), facts)
            if existing is None:
                facts[incoming.fact_id] = incoming
                return incoming
            if not contradicts(existing.value, incoming.value):
                existing.confidence = max(existing.confidence, incoming.confidence)
                existing.last_verified_at = utc_now_iso()
                existing.reinforcement_count += 1
                if existing.status != "disputed":
                    existing.status = "active"
                existing.evidence_refs = self._merge_refs(existing.evidence_refs, incoming.evidence_refs)
            else:
                self._record_dispute(existing, incoming)
            return existing

        return self._transaction(mutation)

    def _record_dispute(self, existing: SemanticFact, incoming: SemanticFact) -> None:
        sides: dict[str, DisputedAlternative] = {}
        alternatives = [
            DisputedAlternative(
                value=existing.value,
                confidence=existing.confidence,
                evidence_refs=existing.evidence_refs,
            ),
            *existing.disputed_alternatives,
            DisputedAlternative(
                value=incoming.value,
                confidence=incoming.confidence,
                evidence_refs=incoming.evidence_refs,
            ),
        ]
        for alternative in alternatives:
            prior = sides.get(alternative.value)
            if prior is None:
                sides[alternative.value] = alternative
            else:
                prior.confidence = max(prior.confidence, alternative.confidence)
                prior.evidence_refs = self._merge_refs(prior.evidence_refs, alternative.evidence_refs)
        ordered = sorted(sides.values(), key=lambda side: side.confidence, reverse=True)
        primary = ordered[0]
        existing.status = "disputed"
        existing.last_verified_at = utc_now_iso()
        existing.value = primary.value
        existing.confidence = primary.confidence
        existing.evidence_refs = primary.evidence_refs
        existing.disputed_alternatives = ordered[1:]

    def get(self, fact_id: str) -> SemanticFact | None:
        self._refresh()
        return self._facts.get(fact_id)

    def all(self) -> list[SemanticFact]:
        self._refresh()
        return list(self._facts.values())

    def active(self) -> list[SemanticFact]:
        self._refresh()
        return [fact for fact in self._facts.values() if fact.status == "active"]

    def maintain(self, *, stale_days: float | None = None, reference_ts: str | None = None) -> int:
        from .decay import DEFAULT_STALE_DAYS, is_stale

        threshold = DEFAULT_STALE_DAYS if stale_days is None else stale_days

        def mutation(facts: dict[str, SemanticFact]) -> int:
            changed = 0
            for fact in facts.values():
                if fact.status == "active" and is_stale(
                    fact, stale_days=threshold, reference_ts=reference_ts,
                ):
                    fact.status = "stale"
                    changed += 1
            return changed

        return self._transaction(mutation)

    def record_citation(self, fact_id: str) -> bool:
        def mutation(facts: dict[str, SemanticFact]) -> bool:
            fact = facts.get(fact_id)
            if fact is None:
                return False
            fact.citation_count += 1
            return True

        return self._transaction(mutation)

    def _mutate_fact(
        self,
        fact_id: str,
        mutation: Callable[[SemanticFact], None],
    ) -> SemanticFact | None:
        def apply(facts: dict[str, SemanticFact]) -> SemanticFact | None:
            fact = facts.get(fact_id)
            if fact is None:
                return None
            mutation(fact)
            return fact

        return self._transaction(apply)

    def recall(
        self,
        query: str,
        *,
        top_k: int = 5,
        context_budget_chars: int = 1200,
        reference_ts: str | None = None,
    ) -> list[MemoryRecall]:
        from .citation_rank import citation_multiplier
        from .decay import apply_decay

        query_terms = _terms(query)
        recalls: list[MemoryRecall] = []
        for fact in self.active():
            decayed = apply_decay(fact, reference_ts=reference_ts)
            haystack = _terms(f"{fact.entity_name} {fact.field} {fact.value}")
            overlap = len(query_terms & haystack)
            if overlap <= 0:
                continue
            score = round(
                decayed * (1.0 + min(overlap, 4) * 0.1) * citation_multiplier(fact), 4,
            )
            recalls.append(MemoryRecall(
                fact=fact,
                decayed_confidence=round(decayed, 4),
                score=score,
                reason=f"{overlap} query term(s) matched",
            ))
        recalls.sort(key=lambda recall: recall.score, reverse=True)
        output: list[MemoryRecall] = []
        used = 0
        for recall in recalls[:top_k]:
            size = len(recall.fact.entity_name) + len(recall.fact.field) + len(recall.fact.value) + 16
            if output and used + size > context_budget_chars:
                break
            if size > context_budget_chars:
                continue
            used += size
            output.append(recall)
        return output

    def _by_key(
        self,
        key: str,
        facts: dict[str, SemanticFact] | None = None,
    ) -> SemanticFact | None:
        source = self._facts if facts is None else facts
        return next((fact for fact in source.values() if fact.key() == key), None)

    @staticmethod
    def _merge_refs(left: list[EvidenceRef], right: list[EvidenceRef]) -> list[EvidenceRef]:
        seen = {ref.ledger_id for ref in left}
        return left + [ref for ref in right if ref.ledger_id not in seen]

def _terms(text: str) -> set[str]:
    return {
        token
        for token in "".join(
            char.casefold() if char.isalnum() else " " for char in text
        ).split()
        if len(token) > 2
    }
