"""Synthesis A: citation-weighted reflective memory ranking."""

from __future__ import annotations

from types import SimpleNamespace

from spider_qwen.evidence.ledger import EvidenceLedger
from spider_qwen.memory.citation_rank import (
    citation_multiplier,
    cited_fact_ids,
    record_citations,
)
from spider_qwen.memory.semantic import SemanticFact, SemanticMemory
from spider_qwen.evidence.models import EvidenceRef


def _fact(name: str = "Acme", citations: int = 0, **kw) -> SemanticFact:
    ref = EvidenceRef(ledger_id="ev_seed", url="https://acme.sg",
                      snippet_hash="x", retrieved_at="2026-06-01T00:00:00+00:00")
    return SemanticFact(entity_type="vendor", entity_name=name, field="quote_channel",
                        value="sales@acme.sg", confidence=0.8, evidence_refs=[ref],
                        citation_count=citations, **kw)


def test_multiplier_is_one_at_zero_and_logarithmic():
    assert citation_multiplier(_fact()) == 1.0
    assert citation_multiplier(_fact(citations=1)) == 1.2
    assert citation_multiplier(_fact(citations=3)) == 1.4
    assert citation_multiplier(_fact(citations=7)) == 1.6
    # Monotonic but sub-linear: a runaway favorite cannot drown out fresh facts.
    assert citation_multiplier(_fact(citations=100)) < 2.4


def test_cited_fact_ids_only_counts_semantic_memory_rows_once():
    ledger = EvidenceLedger("run_cit")
    page_ref = ledger.record(source_tool="tinyfish_fetch", url="https://acme.sg",
                             snippet="page")
    mem_ref = ledger.record(source_tool="semantic_memory", url="semantic-memory",
                            snippet="sales@acme.sg", metadata={"fact_id": "fact_abc"})
    cand_a = SimpleNamespace(evidence_refs=[page_ref, mem_ref])
    cand_b = SimpleNamespace(evidence_refs=[mem_ref])  # same fact reused
    assert cited_fact_ids(ledger, [cand_a, cand_b]) == {"fact_abc"}


def test_record_citations_increments_and_persists(tmp_path):
    memory = SemanticMemory(state_dir=tmp_path)
    fact = memory.upsert(_fact())
    ledger = EvidenceLedger("run_cit")
    mem_ref = ledger.record(source_tool="semantic_memory", url="semantic-memory",
                            snippet="sales@acme.sg", metadata={"fact_id": fact.fact_id})
    cand = SimpleNamespace(evidence_refs=[mem_ref])

    assert record_citations(memory, ledger, [cand]) == 1
    assert memory.get(fact.fact_id).citation_count == 1
    # Unknown fact ids are not credited.
    ghost = ledger.record(source_tool="semantic_memory", url="semantic-memory",
                          snippet="x", metadata={"fact_id": "fact_ghost"})
    assert record_citations(memory, ledger, [SimpleNamespace(evidence_refs=[ghost])]) == 0
    # citation_count survives reload (persisted schema field).
    reloaded = SemanticMemory(state_dir=tmp_path)
    assert reloaded.get(fact.fact_id).citation_count == 1


def test_recall_ranks_cited_fact_above_equal_uncited_fact(tmp_path):
    memory = SemanticMemory(state_dir=tmp_path)
    cited = memory.upsert(_fact(name="Acme Cleaning"))
    uncited = memory.upsert(_fact(name="Acme Catering"))
    for _ in range(3):
        memory.record_citation(cited.fact_id)

    recalls = memory.recall("acme quote channel")
    assert [r.fact.fact_id for r in recalls][:2] == [cited.fact_id, uncited.fact_id]
    assert recalls[0].score > recalls[1].score


def test_zero_citations_leaves_recall_scores_unchanged(tmp_path):
    memory = SemanticMemory(state_dir=tmp_path)
    fact = memory.upsert(_fact())
    (recall,) = memory.recall("acme quote channel")
    # decayed * (1 + overlap*0.1) * 1.0 -- multiplier must be a no-op at zero.
    assert recall.fact.fact_id == fact.fact_id
    assert citation_multiplier(fact) == 1.0
