"""Semantic-memory promotion needs fresh, independent page evidence."""

from __future__ import annotations

from spider_qwen.evidence.ledger import EvidenceLedger
from spider_qwen.evidence.models import EvidenceRef
from spider_qwen.memory.promotion import should_promote_contact
from spider_qwen.modes.contracts import QuoteChannel, QuoteChannelType, ServiceCandidate


def _ref(ledger_id: str, url: str) -> EvidenceRef:
    return EvidenceRef(ledger_id=ledger_id, url=url, snippet_hash="0" * 64,
                       retrieved_at="2026-09-01T00:00:00Z")


def test_two_rows_from_one_site_are_one_source():
    refs = [_ref("ev_1", "https://acme.sg/contact"), _ref("ev_2", "https://www.acme.sg/about")]
    assert not should_promote_contact(evidence_refs=refs, confidence=0.5, domain_match=False)
    refs.append(_ref("ev_3", "https://directory.example/acme"))
    assert should_promote_contact(evidence_refs=refs, confidence=0.5, domain_match=False)


def _promote(tmp_path, source_tool: str, page_url: str):
    from spider_qwen.agent.controller import Controller

    controller = Controller(offline=True, state_dir=str(tmp_path))
    ledger = EvidenceLedger("run_promote", None)
    ref = ledger.record(source_tool=source_tool, url=page_url, snippet="sales@acme.sg",
                        text="Email sales@acme.sg for a quotation.")
    cand = ServiceCandidate(
        vendor_name="Acme", website="https://acme.sg", evidence_refs=[ref],
        quote_channel=QuoteChannel(type=QuoteChannelType.CONTACT_EMAIL,
                                   value="sales@acme.sg", evidence_ref=ref),
    )
    controller._persist_semantic([cand], "run_promote", None, ledger)
    return [f for f in controller._semantic_memory().all() if f.field == "quote_channel"]


def test_quote_channel_found_on_vendor_site_is_promoted(tmp_path):
    assert [f.value for f in _promote(tmp_path, "tinyfish_fetch", "https://acme.sg/contact")] == [
        "sales@acme.sg"]


def test_recalled_quote_channel_is_not_re_promoted(tmp_path):
    assert _promote(tmp_path, "semantic_memory", "https://acme.sg") == []


def test_single_off_site_quote_channel_is_not_promoted(tmp_path):
    assert _promote(tmp_path, "tinyfish_fetch", "https://directory.example/acme") == []
