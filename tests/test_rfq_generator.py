from __future__ import annotations

from spider_qwen.evidence.models import EvidenceRef
from spider_qwen.modes.contracts import QuoteChannel, QuoteChannelType, ServiceCandidate
from spider_qwen.rfq.generator import RFQGenerator


def _ref() -> EvidenceRef:
    return EvidenceRef(ledger_id="ev_1", url="https://a.sg", snippet_hash="h", retrieved_at="2026-01-01T00:00:00Z")


def _candidate(with_channel: bool) -> ServiceCandidate:
    qc = QuoteChannel(type=QuoteChannelType.CONTACT_EMAIL, value="sales@a.sg", evidence_ref=_ref()) if with_channel else None
    return ServiceCandidate(
        vendor_name="Example Cleaning Pte Ltd", website="https://a.sg", country="Singapore",
        service_match_score=1.0, service_match_evidence=True, quote_channel=qc, evidence_refs=[_ref()],
    )


def test_complete_draft_has_email_and_checklist():
    draft = RFQGenerator().generate(query="office cleaning Singapore", candidate=_candidate(True), target_country="Singapore")
    assert draft.status == "complete"
    assert draft.rfq_email_template
    assert draft.quote_channel is not None and draft.quote_channel.evidence_ref is not None
    assert len(draft.required_inputs_checklist) >= 4


def test_no_quote_channel_hard_stop():
    draft = RFQGenerator().generate(query="office cleaning Singapore", candidate=_candidate(False), target_country="Singapore")
    assert draft.status == "incomplete"
    assert draft.rfq_email_template == ""
    assert any("quote channel" in a.lower() for a in draft.assumptions_and_limits)


def test_draft_never_claims_to_send():
    draft = RFQGenerator().generate(query="pest control Singapore", candidate=_candidate(True), target_country="Singapore")
    assert any("draft only" in a.lower() for a in draft.assumptions_and_limits)
