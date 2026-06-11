"""Crash-safe incremental evidence persistence: the write-ahead log.

Every record() appends to <state_dir>/evidence/<run_id>.ledger.wal.jsonl, so a
crash mid-run loses at most a partially written final row. persist() supersedes
and removes the WAL; load() falls back to WAL replay when the canonical file
never landed.
"""

from __future__ import annotations

import json

from spider_qwen.evidence.ledger import EvidenceLedger


def _record(ledger: EvidenceLedger, i: int = 0):
    return ledger.record(
        source_tool="tinyfish_search",
        url=f"https://example.com/{i}",
        snippet=f"snippet {i}",
        text=f"page text {i}",
    )


def test_record_appends_wal_and_persist_clears_it(tmp_path):
    ledger = EvidenceLedger("run_wal_basic", tmp_path)
    _record(ledger, 0)
    _record(ledger, 1)
    wal = ledger.wal_path()
    assert wal.exists()
    assert len(wal.read_text(encoding="utf-8").splitlines()) == 2

    target = ledger.persist()
    assert target.exists()
    assert not wal.exists()  # canonical file supersedes the crash log


def test_load_recovers_from_wal_when_persist_never_ran(tmp_path):
    ledger = EvidenceLedger("run_wal_crash", tmp_path)
    ids = [_record(ledger, i).ledger_id for i in range(3)]
    # Simulated crash: the process dies before persist().

    recovered = EvidenceLedger.load("run_wal_crash", tmp_path)
    assert [item.ledger_id for item in recovered.items()] == ids
    assert recovered.verify_chain().ok
    # Recovery is a full ledger: it can persist and publish a commitment.
    recovered.persist()
    assert recovered.path().exists()
    assert not recovered.wal_path().exists()


def test_canonical_file_takes_precedence_over_stale_wal(tmp_path):
    ledger = EvidenceLedger("run_wal_precedence", tmp_path)
    _record(ledger, 0)
    ledger.persist()
    # A stray WAL (e.g. crash after persist of a later tool) must not shadow
    # the canonical file and its published tree head.
    ledger.wal_path().write_text('{"bogus": true}\n', encoding="utf-8")

    loaded = EvidenceLedger.load("run_wal_precedence", tmp_path)
    assert len(loaded) == 1
    assert loaded.published_tree_head() is not None


def test_truncated_wal_tail_is_dropped(tmp_path):
    ledger = EvidenceLedger("run_wal_torn", tmp_path)
    _record(ledger, 0)
    _record(ledger, 1)
    wal = ledger.wal_path()
    lines = wal.read_text(encoding="utf-8").splitlines()
    # Crash mid-append: the final line is half-written.
    wal.write_text(lines[0] + "\n" + lines[1][: len(lines[1]) // 2], encoding="utf-8")

    recovered = EvidenceLedger.load("run_wal_torn", tmp_path)
    assert len(recovered) == 1
    assert recovered.verify_chain().ok


def test_wal_recovery_survives_mid_run_annotation_reseal(tmp_path):
    ledger = EvidenceLedger("run_wal_reseal", tmp_path)
    first = _record(ledger, 0)
    # Claim rows get verifier verdicts annotated mid-run (chain resealed), and
    # a replan round records more evidence afterwards. The WAL holds
    # record-time dumps, so recovery must reseal instead of reporting tampering.
    ledger.annotate(first.ledger_id, {"verified": True, "verifier_score": 0.9})
    _record(ledger, 1)

    recovered = EvidenceLedger.load("run_wal_reseal", tmp_path)
    assert len(recovered) == 2
    assert recovered.verify_chain().ok
    # WAL rows predate the annotation; recovery is the pre-verification record.
    assert "verified" not in recovered.items()[0].metadata


def test_no_state_dir_means_no_wal():
    ledger = EvidenceLedger("run_wal_none")
    _record(ledger, 0)
    assert ledger.wal_path() is None


def test_wal_lines_are_valid_evidence_items(tmp_path):
    ledger = EvidenceLedger("run_wal_shape", tmp_path)
    ref = _record(ledger, 0)
    raw = json.loads(ledger.wal_path().read_text(encoding="utf-8").splitlines()[0])
    assert raw["ledger_id"] == ref.ledger_id
    assert raw["chain_hash"]
