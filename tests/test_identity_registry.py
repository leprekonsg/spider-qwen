from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from spider_qwen.agent.controller import Controller
from spider_qwen.evidence.ledger import EvidenceLedger
from spider_qwen.evidence.models import EvidenceRef
from spider_qwen.identity import stable_supplier_id
from spider_qwen.identity_registry import SupplierIdentityRegistry
from spider_qwen.memory.semantic import MemoryRecall, SemanticFact, SemanticMemory
from spider_qwen.modes.contracts import ServiceCandidate


def _ref(number: int) -> EvidenceRef:
    return EvidenceRef(
        ledger_id=f"ev_{number}",
        url=f"https://evidence{number}.example/supplier",
        snippet_hash=f"hash_{number}",
        retrieved_at="2026-09-05T00:00:00Z",
    )


def test_registry_requires_evidence_and_never_infers_same_name_aliases(tmp_path):
    registry = SupplierIdentityRegistry(tmp_path)
    first = stable_supplier_id("Same Name Pte Ltd", "https://first-supplier.sg")
    second = stable_supplier_id("Same Name Pte Ltd", "https://second-supplier.sg")

    assert not registry.equivalent(first, second)
    with pytest.raises(ValueError, match="evidence_ref"):
        registry.approve_alias(first, second, evidence_refs=[])
    assert registry.aliases() == []


def test_approved_legal_and_trading_name_transition_is_durable(tmp_path):
    provisional = stable_supplier_id("Old Trading Name", country="Singapore")
    canonical = stable_supplier_id(
        "New Trading Name", "https://supplier.example.sg", legal_name="Supplier Holdings Pte Ltd"
    )
    registry = SupplierIdentityRegistry(tmp_path)
    registry.approve_alias(provisional, canonical, evidence_refs=[_ref(1)])

    reloaded = SupplierIdentityRegistry(tmp_path)
    assert reloaded.resolve(provisional) == canonical
    assert reloaded.equivalent(provisional, canonical)
    assert reloaded.aliases()[0].evidence_refs == [_ref(1)]


def test_alias_chain_resolution_is_approval_order_invariant(tmp_path):
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first = SupplierIdentityRegistry(first_dir)
    first.approve_alias("sup_provisional", "sup_trading", evidence_refs=[_ref(1)])
    first.approve_alias("sup_trading", "sup_legal", evidence_refs=[_ref(2)])

    second = SupplierIdentityRegistry(second_dir)
    second.approve_alias("sup_trading", "sup_legal", evidence_refs=[_ref(2)])
    second.approve_alias("sup_provisional", "sup_trading", evidence_refs=[_ref(1)])

    for registry in (first, second):
        assert registry.resolve("sup_provisional") == "sup_legal"
        assert registry.resolve("sup_trading") == "sup_legal"
        assert registry.equivalent("sup_provisional", "sup_legal")
    first_rows = [(row.alias_supplier_id, row.canonical_supplier_id) for row in first.aliases()]
    second_rows = [(row.alias_supplier_id, row.canonical_supplier_id) for row in second.aliases()]
    assert first_rows == second_rows


def test_registry_rejects_cycles_and_conflicting_remaps(tmp_path):
    registry = SupplierIdentityRegistry(tmp_path)
    registry.approve_alias("sup_a", "sup_b", evidence_refs=[_ref(1)])
    with pytest.raises(ValueError, match="cycle"):
        registry.approve_alias("sup_b", "sup_a", evidence_refs=[_ref(2)])
    with pytest.raises(ValueError, match="already resolves"):
        registry.approve_alias("sup_a", "sup_c", evidence_refs=[_ref(3)])


def test_controller_accepts_an_explicitly_approved_supplier_alias(tmp_path):
    registry = SupplierIdentityRegistry(tmp_path)
    registry.approve_alias("sup_old_name", "sup_legal", evidence_refs=[_ref(1)])
    controller = Controller(
        state_dir=tmp_path,
        persist=True,
        offline=True,
        identity_registry=registry,
    )
    candidate = ServiceCandidate(
        supplier_id="sup_legal",
        vendor_name="Supplier Holdings Pte Ltd",
        website="https://supplier.example.sg",
        service_match_score=1.0,
        service_match_evidence=True,
        evidence_refs=[_ref(2)],
    )
    recall = MemoryRecall(
        fact=SemanticFact(
            entity_type="vendor",
            entity_name="Old Trading Name",
            supplier_id="sup_old_name",
            field="quote_channel",
            value="sales@supplier.example.sg",
            confidence=0.9,
            evidence_refs=[_ref(3)],
        ),
        decayed_confidence=0.8,
        score=0.9,
    )

    enriched = controller._apply_memory_recalls(
        SimpleNamespace(ledger=EvidenceLedger("run_alias")),
        [candidate],
        [recall],
    )

    assert enriched[0].quote_channel is not None
    assert enriched[0].quote_channel.value == "sales@supplier.example.sg"


def test_legacy_name_only_semantic_fact_remains_unbound_on_load(tmp_path):
    path = tmp_path / "memory" / "semantic.json"
    path.parent.mkdir(parents=True)
    raw = SemanticFact(
        entity_type="vendor",
        entity_name="Legacy Supplier",
        field="quote_channel",
        value="sales@legacy.example",
        evidence_refs=[_ref(1)],
    ).model_dump(mode="json")
    raw.pop("supplier_id")
    path.write_text(json.dumps([raw]), encoding="utf-8")

    loaded = SemanticMemory(tmp_path).all()
    assert loaded[0].supplier_id is None
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert "supplier_id" not in persisted[0]
