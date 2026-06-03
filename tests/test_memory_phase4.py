"""T-4.1: Reflections + MemoryBank (Ebbinghaus) decay + A-MEM Zettelkasten notes.

Acceptance: `spider-qwen memory show` prints active facts with TTL/last_verified/
confidence (covered by test_enhancements); MemoryBank decay grows stability on
re-access and shrinks it per contradiction; reflections distil insights from
recent facts/episodes; each ledger-derived note carries Zettelkasten attributes
and 1-3 agentic links.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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


# --- MemoryBank decay -------------------------------------------------------

def test_decay_reduces_to_half_life_baseline_when_unreinforced():
    # exp(-age/S) must reproduce the old 0.5**(age/half_life) at zero reinforcement.
    fact = _aged_fact(90.0)  # one half-life old
    assert apply_decay(fact) == 0.5


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
