"""T-R.3: pluggable memory recall backend with automatic lexical fallback.

Two tiers:
- ``LexicalRecallBackend`` (default) -- the existing deterministic term-overlap
  recall over ``SemanticMemory``. Offline, zero extra dependency.
- ``VectorRecallBackend`` (optional) -- ``sqlite-vec`` KNN over ``text-embedding-v4``
  vectors. Used ONLY when the SQLite extension loads AND an embedder is supplied.

``build_recall_backend`` prefers vectors when possible and transparently falls back
to lexical when the extension or the embedder (API key) is unavailable, or if the
vector backend fails to construct. The offline path therefore never needs the
extension. The hot offline default stays deterministic and network-free.
"""

from __future__ import annotations

from typing import Callable, Protocol

from .decay import apply_decay
from .semantic import MemoryRecall, SemanticMemory

Embedder = Callable[[str], list[float]]


class RecallBackend(Protocol):
    name: str

    def recall(self, query: str, *, top_k: int = 5, context_budget_chars: int = 1200) -> list[MemoryRecall]:
        ...


class LexicalRecallBackend:
    name = "lexical"

    def __init__(self, memory: SemanticMemory) -> None:
        self.memory = memory

    def recall(self, query: str, *, top_k: int = 5, context_budget_chars: int = 1200) -> list[MemoryRecall]:
        return self.memory.recall(query, top_k=top_k, context_budget_chars=context_budget_chars)


def sqlite_vec_available() -> bool:
    """True only if sqlite-vec can be imported AND extension loading is permitted."""
    try:
        import sqlite3

        import sqlite_vec  # noqa: F401  (presence check)

        conn = sqlite3.connect(":memory:")
        try:
            conn.enable_load_extension(True)  # some CPython builds omit this
        finally:
            conn.close()
        return True
    except Exception:
        return False


class VectorRecallBackend:
    """sqlite-vec KNN recall over embedded facts. Constructed only when available."""

    name = "vector"

    def __init__(self, memory: SemanticMemory, embedder: Embedder, *, db_path: str = ":memory:", dim: int = 768) -> None:
        import sqlite3

        import sqlite_vec

        self.memory = memory
        self.embedder = embedder
        self.dim = dim
        self.conn = sqlite3.connect(db_path)
        self.conn.enable_load_extension(True)
        sqlite_vec.load(self.conn)
        self.conn.enable_load_extension(False)
        self.conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_mem USING vec0(embedding float[{dim}], fact_id TEXT)"
        )
        self._serialize = sqlite_vec.serialize_float32
        self._index()

    def _index(self) -> None:
        for fact in self.memory.active():
            vector = self.embedder(f"{fact.entity_name} {fact.field} {fact.value}")
            self.conn.execute(
                "INSERT INTO vec_mem(embedding, fact_id) VALUES (?, ?)",
                (self._serialize(vector), fact.fact_id),
            )
        self.conn.commit()

    def recall(self, query: str, *, top_k: int = 5, context_budget_chars: int = 1200) -> list[MemoryRecall]:
        vector = self.embedder(query)
        rows = self.conn.execute(
            "SELECT fact_id, distance FROM vec_mem WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (self._serialize(vector), top_k),
        ).fetchall()
        hits: list[MemoryRecall] = []
        for fact_id, distance in rows:
            fact = self.memory.get(fact_id)
            if fact is None or fact.status != "active":
                continue
            hits.append(MemoryRecall(
                fact=fact,
                decayed_confidence=round(apply_decay(fact), 4),
                score=round(1.0 / (1.0 + float(distance)), 4),
                reason="vector knn",
            ))
        return hits


def rfq_eligible(recalls: list[MemoryRecall], *, allow_disputed: bool = False) -> list[MemoryRecall]:
    """Recalls allowed to enrich an RFQ-bound candidate (boundary defense-in-depth).

    Guardrail (hard rule): a fact with ``status='disputed'`` must never appear in a
    generated RFQ draft. Active facts always pass; disputed facts pass ONLY when
    policy explicitly opts in (``allow_disputed``); stale facts never enrich an RFQ.

    NOTE: the PRIMARY exclusion is upstream -- ``SemanticMemory.recall`` returns only
    ``active()`` facts, so the standard recall path never surfaces a disputed/stale
    fact here. This helper is a defense-in-depth re-check at the RFQ boundary
    (exercised directly by the guardrail tests); the ``allow_disputed`` opt-in only
    takes effect if a recall backend ever surfaces non-active facts.
    """
    out: list[MemoryRecall] = []
    for r in recalls:
        if r.fact.status == "active":
            out.append(r)
        elif r.fact.status == "disputed" and allow_disputed:
            out.append(r)
    return out


def build_recall_backend(
    memory: SemanticMemory,
    *,
    embedder: Embedder | None = None,
    prefer_vector: bool = True,
    db_path: str = ":memory:",
    dim: int = 768,
) -> RecallBackend:
    """Return the best available recall backend, falling back to lexical."""
    if prefer_vector and embedder is not None and sqlite_vec_available():
        try:
            return VectorRecallBackend(memory, embedder, db_path=db_path, dim=dim)
        except Exception:
            pass  # any failure (extension/version/API) -> deterministic lexical
    return LexicalRecallBackend(memory)
