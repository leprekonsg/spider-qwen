from __future__ import annotations

from spider_qwen.evidence.dedupe import canonicalize_url, dedupe_items
from spider_qwen.evidence.ledger import EvidenceLedger
from spider_qwen.evidence.models import EvidenceItem


def test_record_returns_ref_and_persists_in_memory():
    ledger = EvidenceLedger("run_test")
    ref = ledger.record(source_tool="mock", url="https://x.sg", snippet="hello world")
    assert ref.ledger_id.startswith("ev_")
    assert ledger.get(ref.ledger_id) is not None
    assert len(ledger) == 1


def test_snippet_hash_is_set():
    item = EvidenceItem(source_tool="mock", url="https://x.sg", snippet="abc")
    assert item.snippet_hash and len(item.snippet_hash) == 64


def test_url_canonicalization_strips_tracking():
    a = canonicalize_url("https://www.Example.com/path/?utm_source=g&id=1")
    b = canonicalize_url("http://example.com/path?id=1")
    assert a == b


def test_dedupe_collapses_identical_text():
    items = [
        EvidenceItem(source_tool="mock", url="https://a.sg", snippet="same", text_hash="t1"),
        EvidenceItem(source_tool="mock", url="https://b.sg", snippet="same2", text_hash="t1"),
    ]
    assert len(dedupe_items(items)) == 1


def test_persist_and_load(tmp_path):
    ledger = EvidenceLedger("run_persist", state_dir=tmp_path)
    ref = ledger.record(source_tool="mock", url="https://x.sg", snippet="data", text="full body")
    path = ledger.persist()
    assert path is not None and path.exists()

    reloaded = EvidenceLedger.load("run_persist", state_dir=tmp_path)
    assert reloaded.get(ref.ledger_id) is not None
