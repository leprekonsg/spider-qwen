"""T-4.1: Reflections + MemoryBank (Ebbinghaus) decay + A-MEM Zettelkasten notes.

Acceptance: `spider-qwen memory show` prints active facts with TTL/last_verified/
confidence (covered by test_enhancements); MemoryBank decay grows stability on
re-access and shrinks it per contradiction; reflections distil insights from
recent facts/episodes; each ledger-derived note carries Zettelkasten attributes
and 1-3 agentic links.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context
from datetime import datetime, timedelta, timezone

import pytest

from spider_qwen.evidence.models import EvidenceRef, utc_now_iso
from spider_qwen.memory.decay import apply_decay, memory_stability_days
from spider_qwen.memory.episodic import EpisodicRecord
from spider_qwen.memory.notes import Note, NoteStore
from spider_qwen.memory.reflections import Reflection, ReflectionEngine
from spider_qwen.memory.semantic import SemanticFact, SemanticMemory


def _ref(lid: str) -> EvidenceRef:
    return EvidenceRef(ledger_id=lid, url=f"https://{lid}.example", snippet_hash="h", retrieved_at=utc_now_iso())


def _aged_fact(days: float, **kw) -> SemanticFact:
    base = dict(
        entity_type="vendor", entity_name="Acme", field="quote_channel",
        value="sales@acme.sg", confidence=1.0, evidence_refs=[_ref("ev_1")],
        last_verified_at=(datetime.now(timezone.utc) - timedelta(days=days)).isoformat(),
    )
    base.update(kw)
    return SemanticFact(**base)


def _process_memory_insert(args: tuple[str, int]) -> str:
    state_dir, index = args
    memory = SemanticMemory(state_dir)
    fact = memory.upsert(SemanticFact(
        entity_type="vendor",
        entity_name=f"Process Vendor {index}",
        supplier_id=f"sup_process_{index}",
        field="quote_channel",
        value=f"process{index}@example.sg",
        evidence_refs=[_ref(f"ev_process_{index}")],
    ))
    return fact.fact_id


# --- MemoryBank decay -------------------------------------------------------

def test_decay_reduces_to_half_life_baseline_when_unreinforced():
    # exp(-age/S) must reproduce the old 0.5**(age/half_life) at zero reinforcement.
    fact = _aged_fact(90.0)  # one half-life old
    assert apply_decay(fact) == 0.5


def test_decay_uses_reference_timestamp_for_reproducibility():
    fact = SemanticFact(
        entity_type="vendor",
        entity_name="Acme",
        field="quote_channel",
        value="sales@acme.sg",
        confidence=1.0,
        evidence_refs=[_ref("ev_1")],
        last_verified_at="2026-01-01T00:00:00+00:00",
    )
    assert apply_decay(fact, reference_ts="2026-04-01T00:00:00+00:00") == 0.5
    assert apply_decay(fact, reference_ts="2026-01-01T00:00:00+00:00") == 1.0


def test_decay_rejects_malformed_reference_timestamp():
    # A bad reference must fail loud, not silently fall back to wall-clock time.
    with pytest.raises(ValueError, match="reference_ts"):
        apply_decay(_aged_fact(10.0), reference_ts="not-a-timestamp")


def test_reaccess_grows_stability_so_decay_is_slower():
    plain = _aged_fact(90.0)
    reinforced = _aged_fact(90.0, reinforcement_count=2)
    assert memory_stability_days(reinforced) > memory_stability_days(plain)
    assert apply_decay(reinforced) > apply_decay(plain)


def test_each_contradiction_halves_stability_so_decay_is_faster():
    from spider_qwen.evidence.models import DisputedAlternative

    plain = _aged_fact(90.0)
    disputed = _aged_fact(
        90.0,
        disputed_alternatives=[DisputedAlternative(value="other@acme.sg", confidence=0.5, evidence_refs=[_ref("ev_2")])],
    )
    assert memory_stability_days(disputed) == memory_stability_days(plain) / 2.0
    assert apply_decay(disputed) < apply_decay(plain)


def test_corroborating_upsert_reinforces_the_fact(tmp_path):
    mem = SemanticMemory(tmp_path)
    f = mem.upsert(_aged_fact(0.0))
    assert f.reinforcement_count == 0
    again = mem.upsert(_aged_fact(0.0, value="sales@acme.sg", evidence_refs=[_ref("ev_2")]))
    assert again.reinforcement_count == 1  # second corroborating observation re-accesses it


def test_semantic_memory_rejects_schema_drift_on_load(tmp_path):
    path = tmp_path / "memory" / "semantic.json"
    path.parent.mkdir(parents=True)
    fact = _aged_fact(0.0).model_dump(mode="json")
    fact["schema_version"] = "0.0"
    path.write_text(json.dumps([fact]), encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version"):
        SemanticMemory(tmp_path)


def test_vendor_memory_keys_use_supplier_identity(tmp_path):
    memory = SemanticMemory(tmp_path)
    first = _aged_fact(0.0, supplier_id="sup_alpha_sg", value="sales@alpha.sg")
    second = _aged_fact(0.0, supplier_id="sup_alpha_my", value="sales@alpha.my")
    memory.upsert(first)
    memory.upsert(second)
    assert len(memory.all()) == 2
    assert {fact.supplier_id for fact in memory.all()} == {"sup_alpha_sg", "sup_alpha_my"}


def test_legacy_vendor_fact_remains_unbound(tmp_path):
    path = tmp_path / "memory" / "semantic.json"
    path.parent.mkdir(parents=True)
    raw = _aged_fact(0.0).model_dump(mode="json")
    raw.pop("supplier_id")
    path.write_text(json.dumps([raw]), encoding="utf-8")
    loaded = SemanticMemory(tmp_path).all()
    assert len(loaded) == 1
    assert loaded[0].supplier_id is None
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert "supplier_id" not in persisted[0]


def test_concurrent_memory_instances_do_not_lose_updates(tmp_path):
    memories = [SemanticMemory(tmp_path) for _ in range(8)]

    def insert(index: int) -> None:
        memories[index].upsert(_aged_fact(
            0.0,
            entity_name=f"Vendor {index}",
            supplier_id=f"sup_{index}",
            value=f"sales{index}@example.sg",
            evidence_refs=[_ref(f"ev_{index}")],
        ))

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(insert, range(8)))

    stored = SemanticMemory(tmp_path).all()
    assert len(stored) == 8
    assert {fact.supplier_id for fact in stored} == {f"sup_{index}" for index in range(8)}


def test_concurrent_processes_do_not_lose_memory_updates(tmp_path):
    from concurrent.futures import ProcessPoolExecutor

    work = [(str(tmp_path), index) for index in range(4)]
    with ProcessPoolExecutor(max_workers=4, mp_context=get_context("spawn")) as executor:
        fact_ids = list(executor.map(_process_memory_insert, work))
    assert len(set(fact_ids)) == 4
    stored = SemanticMemory(tmp_path).all()
    assert len(stored) == 4
    assert {fact.supplier_id for fact in stored} == {
        f"sup_process_{index}" for index in range(4)
    }


def test_concurrent_citation_updates_are_atomic(tmp_path):
    memory = SemanticMemory(tmp_path)
    fact = memory.upsert(_aged_fact(0.0))
    instances = [SemanticMemory(tmp_path) for _ in range(20)]
    with ThreadPoolExecutor(max_workers=10) as executor:
        assert all(executor.map(lambda item: item.record_citation(fact.fact_id), instances))
    assert SemanticMemory(tmp_path).get(fact.fact_id).citation_count == 20


# --- Reflections ------------------------------------------------------------

def test_reflection_engine_distils_capped_insights_with_evidence():
    from spider_qwen.evidence.models import DisputedAlternative

    facts = [
        _aged_fact(0.0, entity_name="Acme", reinforcement_count=3),
        _aged_fact(200.0, entity_name="Old Co", status="stale"),
        _aged_fact(
            0.0, entity_name="Disp Co", status="disputed",
            disputed_alternatives=[DisputedAlternative(value="x", confidence=0.4, evidence_refs=[_ref("ev_3")])],
        ),
    ]
    episodes = [EpisodicRecord(query="q1", mode="service_quote_required", summary="ok", outcome="success")]
    reflections = ReflectionEngine().reflect(facts, episodes, max_insights=10)

    assert 1 <= len(reflections) <= 10
    assert all(isinstance(r, Reflection) and r.insight for r in reflections)
    kinds = {r.kind for r in reflections}
    assert "dispute" in kinds  # a disputed fact must surface as an actionable insight
    disputes = [r for r in reflections if r.kind == "dispute"]
    assert all(r.evidence_refs for r in disputes)  # insights cite their evidence


def test_reflection_engine_respects_max_insights():
    facts = [_aged_fact(0.0, entity_name=f"V{i}", status="disputed",
                         disputed_alternatives=[]) for i in range(50)]
    reflections = ReflectionEngine().reflect(facts, [], max_insights=5)
    assert len(reflections) <= 5


# --- A-MEM Zettelkasten notes ----------------------------------------------

def test_note_from_fact_carries_zettelkasten_attributes(tmp_path):
    store = NoteStore(tmp_path)
    note = store.add_from_fact(_aged_fact(0.0, entity_name="Acme", value="sales@acme.sg Singapore"))
    assert isinstance(note, Note)
    assert note.entity == "Acme"
    assert note.geo == "Singapore"
    assert note.supply_stage == "sourcing"  # quote_channel -> sourcing
    assert note.risk_flag is False


def test_note_risk_flag_set_for_obsolescence_value(tmp_path):
    store = NoteStore(tmp_path)
    note = store.add_from_fact(
        _aged_fact(0.0, entity_name="Part Co", field="lifecycle_status", value="NRND")
    )
    assert note.risk_flag is True
    assert note.supply_stage == "lifecycle"


def test_notes_form_agentic_links_to_related_notes(tmp_path):
    store = NoteStore(tmp_path)
    first = store.add_from_fact(_aged_fact(0.0, entity_name="Acme", field="quote_channel"))
    second = store.add_from_fact(_aged_fact(0.0, entity_name="Acme", field="price", value="S$10"))
    assert 1 <= len(second.links) <= 3
    assert first.note_id in second.links  # same entity -> linked

    reloaded = NoteStore(tmp_path)
    assert len(reloaded.all()) == 2
