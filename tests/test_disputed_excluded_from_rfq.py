"""Guardrail #3.4: a fact with status='disputed' must never appear in a generated
RFQ draft. The exclusion is explicit and policy-governed via rfq_eligible (the
single enforcement point), not an incidental side effect.
"""

from __future__ import annotations

import json

from spider_qwen.agent.policy import load_policy
from spider_qwen.evidence.models import EvidenceRef
from spider_qwen.memory.recall import rfq_eligible
from spider_qwen.memory.semantic import MemoryRecall, SemanticFact
from spider_qwen.modes.contracts import QuoteChannel, QuoteChannelType, ServiceCandidate
from spider_qwen.rfq.generator import RFQGenerator


def _ref(value: str) -> EvidenceRef:
    return EvidenceRef(ledger_id=f"ev_{abs(hash(value)) % 10**8}", url="https://x.example",
                       snippet_hash="0" * 64, retrieved_at="2026-06-03T00:00:00+00:00")


def _recall(value: str, *, status: str, vendor: str) -> MemoryRecall:
    fact = SemanticFact(
        entity_type="vendor", entity_name=vendor, field="quote_channel",
        value=value, confidence=0.8, status=status, evidence_refs=[_ref(value)],
    )
    return MemoryRecall(fact=fact, decayed_confidence=0.8, score=0.9)


def test_rfq_eligible_excludes_disputed_by_default():
    active = _recall("sales@active.example", status="active", vendor="Active Vendor")
    disputed = _recall("disputed@bad.example", status="disputed", vendor="Bad Source")
    stale = _recall("old@stale.example", status="stale", vendor="Stale Vendor")
    eligible = rfq_eligible([active, disputed, stale])
    assert [r.fact.value for r in eligible] == ["sales@active.example"]


def test_allow_disputed_flag_opts_disputed_back_in():
    disputed = _recall("disputed@bad.example", status="disputed", vendor="Bad Source")
    assert rfq_eligible([disputed], allow_disputed=False) == []
    assert rfq_eligible([disputed], allow_disputed=True) == [disputed]


def test_disputed_fact_filtered_at_eligibility_boundary_not_in_rfq():
    # The RFQ generator has no disputed-status logic of its own; the exclusion is
    # enforced at the eligibility boundary (rfq_eligible / upstream active-only
    # recall) so a disputed fact never reaches the candidate that feeds the draft.
    # This proves that boundary keeps the disputed value out of the rendered draft.
    active = _recall("sales@active.example", status="active", vendor="Active Vendor")
    disputed = _recall("disputed@bad.example", status="disputed", vendor="Active Vendor")
    chosen = rfq_eligible([disputed, active])  # disputed dropped; active survives
    assert [r.fact.value for r in chosen] == ["sales@active.example"]

    fact = chosen[0].fact
    cand = ServiceCandidate(
        vendor_name="Active Vendor",
        quote_channel=QuoteChannel(type=QuoteChannelType.CONTACT_EMAIL, value=fact.value,
                                   evidence_ref=fact.evidence_refs[0]),
        evidence_refs=list(fact.evidence_refs),
        service_match_score=0.9, service_match_evidence=True, checklist_completeness=0.9,
    )
    draft = RFQGenerator().generate(query="office cleaning Singapore", candidate=cand)
    blob = json.dumps(draft.model_dump(mode="json"))
    assert "disputed@bad.example" not in blob
    assert "sales@active.example" in blob


def test_policy_default_excludes_disputed_from_rfq():
    """The previously-dead allow_disputed_facts_in_rfq flag defaults to False."""
    assert load_policy().allow_disputed_facts_in_rfq is False
