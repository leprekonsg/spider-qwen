"""T-2.3: cross-source contradiction -> disputed, retaining BOTH spans.

Acceptance: a Mouser-active vs DigiKey-NRND pair becomes a disputed record that
keeps both values and both evidence spans (the conflict policy reuses semantic
memory's existing disputed status rather than introducing a parallel store).
"""

from __future__ import annotations

from spider_qwen.evidence.models import EvidenceRef, utc_now_iso
from spider_qwen.memory.promotion import contradicts
from spider_qwen.memory.semantic import SemanticFact, SemanticMemory


def _ref(lid: str) -> EvidenceRef:
    return EvidenceRef(ledger_id=lid, url=f"https://{lid}.example",
                       snippet_hash="h", retrieved_at=utc_now_iso())


def _fact(value: str, confidence: float, ref_id: str) -> SemanticFact:
    return SemanticFact(
        entity_type="part", entity_name="ATMEGA48A", field="lifecycle_status",
        value=value, confidence=confidence, evidence_refs=[_ref(ref_id)],
    )


def test_contradicts_normalizes_case_and_punctuation():
    assert contradicts("active", "NRND")
    assert not contradicts("active", "Active ")
    assert not contradicts("end-of-life", "end of life")


def test_conflicting_sources_become_disputed_retaining_both_spans(tmp_path):
    mem = SemanticMemory(tmp_path)
    mem.upsert(_fact("active", 0.85, "ev_mouser"))       # Mouser: active
    fact = mem.upsert(_fact("NRND", 0.85, "ev_digikey"))  # DigiKey: NRND

    assert fact.status == "disputed"
    values = {fact.value} | {alt.value for alt in fact.disputed_alternatives}
    assert values == {"active", "NRND"}
    ref_ids = {r.ledger_id for r in fact.evidence_refs}
    ref_ids |= {r.ledger_id for alt in fact.disputed_alternatives for r in alt.evidence_refs}
    assert {"ev_mouser", "ev_digikey"} <= ref_ids


def test_disputed_record_survives_reload(tmp_path):
    mem = SemanticMemory(tmp_path)
    mem.upsert(_fact("active", 0.85, "ev_mouser"))
    mem.upsert(_fact("NRND", 0.85, "ev_digikey"))

    reloaded = SemanticMemory(tmp_path)
    fact = reloaded._by_key("part:atmega48a:lifecycle_status")
    assert fact is not None and fact.status == "disputed"
    assert {fact.value} | {alt.value for alt in fact.disputed_alternatives} == {"active", "NRND"}


def test_agreeing_sources_stay_active(tmp_path):
    mem = SemanticMemory(tmp_path)
    mem.upsert(_fact("active", 0.8, "ev_a"))
    fact = mem.upsert(_fact("active", 0.9, "ev_b"))
    assert fact.status == "active"
    assert fact.disputed_alternatives == []
    assert {r.ledger_id for r in fact.evidence_refs} == {"ev_a", "ev_b"}


def test_case_variant_value_is_not_a_dispute(tmp_path):
    mem = SemanticMemory(tmp_path)
    mem.upsert(_fact("active", 0.8, "ev_a"))
    fact = mem.upsert(_fact("Active", 0.8, "ev_b"))  # same value, different case
    assert fact.status == "active"
    assert fact.disputed_alternatives == []


def test_higher_confidence_contradiction_still_disputed_retaining_weaker_span(tmp_path):
    # A clearly more confident contradicting claim must NOT silently overwrite and
    # discard the weaker span; it becomes the primary value of a disputed record.
    mem = SemanticMemory(tmp_path)
    mem.upsert(_fact("active", 0.60, "ev_low"))         # weaker source: active
    fact = mem.upsert(_fact("NRND", 0.90, "ev_high"))   # stronger source: NRND

    assert fact.status == "disputed"
    assert fact.value == "NRND"                         # highest-confidence side is primary
    assert {fact.value} | {a.value for a in fact.disputed_alternatives} == {"active", "NRND"}
    all_refs = {r.ledger_id for r in fact.evidence_refs}
    all_refs |= {r.ledger_id for a in fact.disputed_alternatives for r in a.evidence_refs}
    assert {"ev_low", "ev_high"} <= all_refs            # weaker span retained, not lost
