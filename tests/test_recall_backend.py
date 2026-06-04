"""T-R.3: optional vector recall backend with automatic lexical fallback.

Default recall is deterministic lexical (offline, zero-dep). A sqlite-vec +
text-embedding-v4 vector backend is used ONLY when the extension loads and an
embedder is supplied; otherwise the factory transparently falls back to lexical.
The unavailable path is the one CI actually exercises (sqlite-vec is not installed).
"""

from __future__ import annotations

from spider_qwen.evidence.models import EvidenceRef, utc_now_iso
from spider_qwen.memory import recall as recall_mod
from spider_qwen.memory.recall import (
    LexicalRecallBackend,
    build_recall_backend,
    sqlite_vec_available,
)
from spider_qwen.memory.semantic import SemanticFact, SemanticMemory


def _memory(tmp_path) -> SemanticMemory:
    mem = SemanticMemory(tmp_path)
    mem.upsert(SemanticFact(
        entity_type="vendor", entity_name="Example Cleaning", field="quote_channel",
        value="sales@example.sg", confidence=0.9,
        evidence_refs=[EvidenceRef(ledger_id="ev_1", url="https://example.sg", snippet_hash="h", retrieved_at=utc_now_iso())],
    ))
    return mem


def test_default_backend_is_lexical_and_recalls_offline(tmp_path):
    backend = build_recall_backend(_memory(tmp_path))  # no embedder
    assert isinstance(backend, LexicalRecallBackend)
    assert backend.name == "lexical"
    hits = backend.recall("Example Cleaning Singapore quotation", top_k=1)
    assert hits and hits[0].fact.value == "sales@example.sg"


def test_sqlite_vec_available_returns_bool(tmp_path):
    assert isinstance(sqlite_vec_available(), bool)


def test_falls_back_to_lexical_when_sqlite_vec_unavailable(tmp_path):
    # An embedder is supplied, but sqlite-vec is not installed here -> lexical.
    embedder = lambda text: [1.0, 0.0]
    backend = build_recall_backend(_memory(tmp_path), embedder=embedder)
    assert backend.name == "lexical"
    assert backend.recall("Example Cleaning quotation", top_k=1)


def test_falls_back_when_vector_backend_construction_raises(tmp_path, monkeypatch):
    # Force "available", but make the vector backend blow up -> must fall back.
    monkeypatch.setattr(recall_mod, "sqlite_vec_available", lambda: True)

    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("extension load failed")

    monkeypatch.setattr(recall_mod, "VectorRecallBackend", _Boom)
    backend = build_recall_backend(_memory(tmp_path), embedder=lambda t: [1.0, 0.0])
    assert backend.name == "lexical"
    assert backend.recall("Example Cleaning quotation", top_k=1)
