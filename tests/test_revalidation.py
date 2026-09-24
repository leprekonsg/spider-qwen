"""Manual revalidation must not re-cite old evidence for a new value."""

from __future__ import annotations

from spider_qwen.evidence.models import EvidenceRef
from spider_qwen.memory.revalidation import Revalidator
from spider_qwen.memory.semantic import SemanticFact, SemanticMemory


def _ref(ledger_id: str) -> EvidenceRef:
    return EvidenceRef(ledger_id=ledger_id, url="https://acme.sg/contact",
                       snippet_hash="0" * 64, retrieved_at="2026-09-01T00:00:00Z")


def _stored(tmp_path) -> tuple[SemanticMemory, SemanticFact]:
    memory = SemanticMemory(state_dir=tmp_path)
    fact = memory.upsert(SemanticFact(
        entity_type="vendor", entity_name="Acme", field="quote_channel",
        value="sales@acme.sg", confidence=0.6, evidence_refs=[_ref("ev_old")],
        last_verified_at="2026-01-01T00:00:00Z",
    ))
    return memory, fact


def test_new_value_with_only_old_refs_is_disputed_not_overwritten(tmp_path):
    memory, fact = _stored(tmp_path)
    refreshed = Revalidator(memory).revalidate(
        fact_id=fact.fact_id, observed_value="quotes@acme.sg",
        evidence_refs=list(fact.evidence_refs), new_confidence=0.9,
    )
    assert refreshed.value == "sales@acme.sg"
    assert refreshed.status == "disputed"


def test_new_value_with_new_ref_and_higher_confidence_replaces(tmp_path):
    memory, fact = _stored(tmp_path)
    refreshed = Revalidator(memory).revalidate(
        fact_id=fact.fact_id, observed_value="quotes@acme.sg",
        evidence_refs=[_ref("ev_new")], new_confidence=0.9,
    )
    assert refreshed.value == "quotes@acme.sg"
    assert [ref.ledger_id for ref in refreshed.evidence_refs] == ["ev_new"]


def test_same_value_with_only_old_refs_does_not_refresh_verification(tmp_path):
    memory, fact = _stored(tmp_path)
    refreshed = Revalidator(memory).revalidate(
        fact_id=fact.fact_id, observed_value="sales@acme.sg",
        evidence_refs=list(fact.evidence_refs), new_confidence=0.9,
    )
    assert refreshed.last_verified_at == "2026-01-01T00:00:00Z"
    assert refreshed.confidence == 0.6
