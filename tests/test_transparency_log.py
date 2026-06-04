"""RFC 6962 Merkle transparency log over the evidence ledger."""

from __future__ import annotations

import hashlib

import pytest

from spider_qwen.evidence.ledger import EvidenceLedger
from spider_qwen.evidence.transparency import (
    CitationProof,
    MerkleLog,
    TreeHead,
    leaf_hash,
    verify_citation,
    verify_consistency,
    verify_inclusion,
)

# Generation (recursive, RFC 6962 section 2.1) and verification (iterative,
# RFC 9162 section 2.1.3/2.1.4) are independent code paths; the exhaustive
# property tests below cross-check one against the other.

LEAVES = [f"chainhash-{i:02d}" for i in range(11)]


def make_log(n: int = len(LEAVES)) -> MerkleLog:
    return MerkleLog("run_test", LEAVES[:n])


def test_empty_tree_root_is_sha256_of_empty_string():
    assert MerkleLog("run_test").root_hash() == hashlib.sha256(b"").hexdigest()


def test_single_leaf_root_is_rfc6962_leaf_hash():
    # MTH({d0}) = SHA-256(0x00 || d0); the canonical empty-leaf vector.
    assert MerkleLog("run_test", [""]).root_hash() == hashlib.sha256(b"\x00").hexdigest()
    assert leaf_hash("") == hashlib.sha256(b"\x00").hexdigest()


def test_inclusion_proofs_verify_for_every_leaf_and_every_tree_size():
    log = make_log()
    for n in range(1, len(LEAVES) + 1):
        root = log.root_hash(n)
        for i in range(n):
            path = log.inclusion_proof(i, n)
            assert verify_inclusion(LEAVES[i], i, n, path, root), (i, n)


def test_inclusion_proof_rejects_wrong_leaf_index_and_tampered_leaf():
    log = make_log()
    root = log.root_hash()
    n = len(LEAVES)
    path = log.inclusion_proof(3)
    assert not verify_inclusion(LEAVES[3], 4, n, path, root)
    assert not verify_inclusion("forged-leaf", 3, n, path, root)
    assert not verify_inclusion(LEAVES[3], 3, n, path[:-1], root)
    assert not verify_inclusion(LEAVES[3], n + 1, n, path, root)


def test_consistency_proofs_verify_for_every_size_pair():
    log = make_log()
    for first in range(1, len(LEAVES) + 1):
        for second in range(first, len(LEAVES) + 1):
            proof = log.consistency_proof(first, second)
            assert verify_consistency(
                first, second, log.root_hash(first), log.root_hash(second), proof
            ), (first, second)


def test_consistency_rejects_forked_log():
    log = make_log(8)
    # A retroactive edit at index 2 produces a different tree whose head is
    # NOT consistent with the original size-5 head: the monitor story.
    forked = MerkleLog("run_test", LEAVES[:2] + ["edited"] + LEAVES[3:8])
    proof = forked.consistency_proof(5, 8)
    assert not verify_consistency(5, 8, log.root_hash(5), forked.root_hash(8), proof)


def test_consistency_same_size_requires_equal_roots_and_empty_proof():
    log = make_log(6)
    assert verify_consistency(6, 6, log.root_hash(6), log.root_hash(6), [])
    assert not verify_consistency(6, 6, log.root_hash(6), log.root_hash(5), [])
    assert not verify_consistency(6, 6, log.root_hash(6), log.root_hash(6), ["junk"])


def test_proof_bounds_raise():
    log = make_log(4)
    with pytest.raises(ValueError):
        log.inclusion_proof(4)
    with pytest.raises(ValueError):
        log.consistency_proof(0)
    with pytest.raises(ValueError):
        log.consistency_proof(2, 9)


def _ledger_with_items(n: int = 4) -> EvidenceLedger:
    ledger = EvidenceLedger("run_tlog")
    for i in range(n):
        ledger.record(source_tool="mock", url=f"https://example.com/{i}",
                      snippet=f"snippet {i}", text=f"page text {i}")
    return ledger


def test_citation_proof_round_trip_from_ledger():
    ledger = _ledger_with_items()
    log = ledger.transparency_log()
    target = ledger.items()[2]
    proof = log.citation_proof(ledger, target.ledger_id)
    assert isinstance(proof, CitationProof)
    assert proof.leaf_index == 2
    assert proof.leaf_data == target.chain_hash
    assert verify_citation(proof)
    # Tampering with the cited item's content breaks the proof.
    tampered = proof.model_copy(update={"leaf_data": "forged"})
    assert not verify_citation(tampered)
    with pytest.raises(KeyError):
        log.citation_proof(ledger, "ev_missing")


def test_merkle_log_and_linear_chain_bind_the_same_digests():
    ledger = _ledger_with_items()
    log = ledger.transparency_log()
    assert [item.chain_hash for item in ledger.items()] == log._leaves
    assert ledger.verify_chain().ok


def test_ledger_persist_includes_tree_head(tmp_path):
    ledger = EvidenceLedger("run_tlog", state_dir=tmp_path)
    ledger.record(source_tool="mock", url="https://example.com", snippet="s")
    path = ledger.persist()
    import json

    payload = json.loads(path.read_text(encoding="utf-8"))
    head = TreeHead.model_validate(payload["tree_head"])
    assert head.tree_size == 1
    assert head.root_hash == ledger.transparency_log().root_hash()
    # Reload still works with the extra key present.
    reloaded = EvidenceLedger.load("run_tlog", tmp_path)
    assert len(reloaded) == 1


def test_signing_requires_crypto_extra_or_signs_and_verifies():
    log = make_log(3)
    head = log.tree_head()
    try:
        from spider_qwen.evidence.transparency import (
            generate_signing_key,
            sign_tree_head,
            verify_signed_tree_head,
        )

        key = generate_signing_key()
    except ImportError as exc:
        assert "spider-qwen[crypto]" in str(exc)
        return
    sth = sign_tree_head(head, key)
    assert verify_signed_tree_head(sth)
    bad = sth.model_copy(update={"head": head.model_copy(update={"tree_size": 99})})
    assert not verify_signed_tree_head(bad)
