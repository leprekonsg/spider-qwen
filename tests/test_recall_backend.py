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


def test_falls_back_when_vector_backend_construction_raises(tmp_path, monkeypatch, caplog):
    # Force "available", but make the vector backend blow up -> must fall back
    # AND say so: which backend serves recall changes ranking behavior, so a
    # silent fallback is a silent failure.
    monkeypatch.setattr(recall_mod, "sqlite_vec_available", lambda: True)

    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("extension load failed")

    monkeypatch.setattr(recall_mod, "VectorRecallBackend", _Boom)
    with caplog.at_level("WARNING", logger="spider_qwen.memory.recall"):
        backend = build_recall_backend(_memory(tmp_path), embedder=lambda t: [1.0, 0.0])
    assert backend.name == "lexical"
    assert any("falling back to lexical" in r.message for r in caplog.records)
    assert backend.recall("Example Cleaning quotation", top_k=1)


def test_vector_backend_applies_citation_multiplier(tmp_path):
    # Parity with the lexical path: switching backends must not silently drop
    # the ledger-supervised citation boost. Runs only where sqlite-vec is
    # installed; the scoring rule itself is what this asserts.
    import pytest

    pytest.importorskip("sqlite_vec")
    from spider_qwen.memory.recall import VectorRecallBackend

    mem = _memory(tmp_path)
    cited = mem.upsert(SemanticFact(
        entity_type="vendor", entity_name="Example Catering", field="quote_channel",
        value="rfq@catering.sg", confidence=0.9,
        evidence_refs=[EvidenceRef(ledger_id="ev_2", url="https://catering.sg",
                                   snippet_hash="h", retrieved_at=utc_now_iso())],
    ))
    for _ in range(3):
        mem.record_citation(cited.fact_id)
    # Identical embeddings -> identical distance -> the citation boost alone
    # must decide the ordering.
    backend = VectorRecallBackend(mem, lambda text: [1.0, 0.0], dim=2)
    hits = backend.recall("example quote channel", top_k=5)
    assert hits and hits[0].fact.fact_id == cited.fact_id
    others = [h for h in hits if h.fact.fact_id != cited.fact_id]
    assert others and hits[0].score > others[0].score
