from __future__ import annotations

import pytest

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


def test_annotate_reseals_chain_once_and_keeps_it_valid():
    ledger = EvidenceLedger("run_annotate")
    a = ledger.record(source_tool="mock", url="https://a.sg", snippet="a")
    b = ledger.record(source_tool="mock", url="https://b.sg", snippet="b")
    before = ledger.get(b.ledger_id).chain_hash

    ledger.annotate(a.ledger_id, {"verified": True, "grade": "high"})
    ledger.annotate(b.ledger_id, {"verified": False})  # batch: still one reseal

    assert ledger.get(a.ledger_id).metadata["grade"] == "high"
    assert ledger.verify_chain().ok  # reader resealed the stale chain
    assert ledger.get(b.ledger_id).chain_hash != before  # final content is bound


def test_record_after_annotate_links_to_resealed_tip():
    ledger = EvidenceLedger("run_annotate_tip")
    a = ledger.record(source_tool="mock", url="https://a.sg", snippet="a")
    ledger.annotate(a.ledger_id, {"verified": True})
    ledger.record(source_tool="mock", url="https://b.sg", snippet="b")
    assert ledger.verify_chain().ok


def test_annotate_refuses_after_tree_head_commitment(tmp_path):
    ledger = EvidenceLedger("run_published", state_dir=tmp_path)
    ref = ledger.record(source_tool="mock", url="https://x.sg", snippet="s")
    ledger.persist()  # publishes the tree_head commitment
    with pytest.raises(ValueError, match="tree_head commitment"):
        ledger.annotate(ref.ledger_id, {"verified": True})

    reloaded = EvidenceLedger.load("run_published", state_dir=tmp_path)
    with pytest.raises(ValueError, match="tree_head commitment"):
        reloaded.annotate(ref.ledger_id, {"verified": True})


def test_annotate_allows_rows_appended_after_commitment(tmp_path):
    # Resume flow: load a published ledger, append a new claim row, verify it.
    # Only the COMMITTED prefix is immutable; the uncommitted suffix must accept
    # verifier write-back, and the prior commitment must stay a consistent
    # prefix of the next one.
    ledger = EvidenceLedger("run_resume", state_dir=tmp_path)
    committed_ref = ledger.record(source_tool="mock", url="https://a.sg", snippet="a")
    ledger.persist()

    reloaded = EvidenceLedger.load("run_resume", state_dir=tmp_path)
    committed_hash = reloaded.get(committed_ref.ledger_id).chain_hash
    new_ref = reloaded.record(source_tool="mock", url="https://b.sg", snippet="b")
    reloaded.annotate(new_ref.ledger_id, {"verified": True, "grade": "high"})

    assert reloaded.get(new_ref.ledger_id).metadata["grade"] == "high"
    assert reloaded.verify_chain().ok
    # Resealing never touches the committed prefix's hashes.
    assert reloaded.get(committed_ref.ledger_id).chain_hash == committed_hash
    # The committed row itself stays immutable.
    with pytest.raises(ValueError, match="tree_head commitment"):
        reloaded.annotate(committed_ref.ledger_id, {"verified": True})

    import json
    payload = json.loads(reloaded.persist().read_text(encoding="utf-8"))
    assert payload["tree_head"]["tree_size"] == 2


def test_annotate_unknown_ledger_id_raises():
    ledger = EvidenceLedger("run_unknown")
    with pytest.raises(KeyError, match="ev_missing"):
        ledger.annotate("ev_missing", {"verified": True})
