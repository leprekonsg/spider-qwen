"""RFC 6962 Merkle transparency log over the evidence ledger."""

from __future__ import annotations

import hashlib

import pytest

from spider_qwen.evidence.ledger import EvidenceLedger
from spider_qwen.evidence.transparency import (
    CitationProof,
    MerkleLog,
    RedactedLeafOpening,
    TreeHead,
    leaf_hash,
    leaf_salt,
    redact_leaf_data,
    verify_citation,
    verify_consistency,
    verify_inclusion,
    verify_redacted_leaf,
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


def test_malformed_proof_elements_verify_false_not_crash():
    # Proofs arrive from untrusted external input: a non-hex element, a short
    # digest, or a bogus root must FAIL verification, never raise.
    log = make_log()
    n = len(LEAVES)
    root = log.root_hash()
    path = log.inclusion_proof(3)
    assert not verify_inclusion(LEAVES[3], 3, n, ["zz" * 32], root)
    assert not verify_inclusion(LEAVES[3], 3, n, ["abc123"], root)
    assert not verify_inclusion(LEAVES[3], 3, n, path, "not-a-root")
    proof = log.consistency_proof(5, n)
    assert not verify_consistency(5, n, log.root_hash(5), root, ["zz" * 32])
    assert not verify_consistency(5, n, "junk", root, proof)
    assert not verify_consistency(5, n, log.root_hash(5), "junk", proof)
    # Uppercase hex is the same digest, not a forgery.
    assert verify_inclusion(LEAVES[3], 3, n, [p.upper() for p in path], root.upper())


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


def test_redacted_merkle_log_commits_to_salted_leaf_without_exposing_chain_hash():
    ledger = _ledger_with_items()
    target = ledger.items()[1]
    log = MerkleLog.from_ledger(ledger, redact_salt="demo-salt")
    proof = log.citation_proof(ledger, target.ledger_id)
    salt = leaf_salt("demo-salt", 1)  # per-leaf: opening leaf 1 unblinds only leaf 1
    commitment = redact_leaf_data(target.chain_hash, salt)

    assert proof.leaf_data == commitment
    assert proof.leaf_data != target.chain_hash
    # Sibling leaves commit under different derived salts.
    assert leaf_salt("demo-salt", 0) != salt
    assert verify_citation(proof)
    assert verify_redacted_leaf(RedactedLeafOpening(
        leaf_data=target.chain_hash,
        salt=salt,
        commitment=proof.leaf_data,
    ))
    assert not verify_redacted_leaf(RedactedLeafOpening(
        leaf_data=target.chain_hash,
        salt="wrong-salt",
        commitment=proof.leaf_data,
    ))
    # Untrusted opening: a leaf that is not a chain-hash digest is invalid,
    # never an error (and cannot equivocate via ambiguous concatenation).
    assert not verify_redacted_leaf(RedactedLeafOpening(
        leaf_data="not-a-digest",
        salt=salt,
        commitment=redact_leaf_data("not-a-digest", salt),
    ))


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


def test_reload_and_repersist_preserves_original_commitment(tmp_path):
    # The tree_head is a point-in-time commitment an external party may have
    # recorded: load + persist of an UNCHANGED ledger must keep it verbatim.
    import json

    ledger = EvidenceLedger("run_tlog", state_dir=tmp_path)
    ledger.record(source_tool="mock", url="https://example.com", snippet="s")
    path = ledger.persist()
    original = json.loads(path.read_text(encoding="utf-8"))["tree_head"]

    reloaded = EvidenceLedger.load("run_tlog", tmp_path)
    reloaded.persist()
    after = json.loads(path.read_text(encoding="utf-8"))["tree_head"]
    assert after == original  # timestamp included

    # Appending an item legitimately advances the commitment.
    reloaded.record(source_tool="mock", url="https://example.com/2", snippet="s2")
    reloaded.persist()
    advanced = json.loads(path.read_text(encoding="utf-8"))["tree_head"]
    assert advanced["tree_size"] == 2
    assert advanced["root_hash"] != original["root_hash"]


def test_load_rejects_ledger_tampered_after_commitment(tmp_path):
    import json

    ledger = EvidenceLedger("run_tlog", state_dir=tmp_path)
    ledger.record(source_tool="mock", url="https://example.com", snippet="s")
    path = ledger.persist()
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["items"][0]["chain_hash"] = "0" * 64  # retroactive edit
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="tree_head commitment"):
        EvidenceLedger.load("run_tlog", tmp_path)


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
    assert sth.schema_version  # persistent model: versioned like the rest
    trusted = sth.public_key  # the anchor a verifier pins out-of-band
    assert verify_signed_tree_head(sth, trusted)
    bad = sth.model_copy(update={"head": head.model_copy(update={"tree_size": 99})})
    assert not verify_signed_tree_head(bad, trusted)


def test_sth_signed_by_attacker_key_fails_against_trust_anchor():
    # An attacker who rewrites the ledger can re-sign with a fresh keypair;
    # the STH then self-validates against its own embedded key. Verification
    # must therefore anchor on the EXPECTED key, not the embedded one.
    pytest.importorskip("cryptography")  # visible SKIP, never a silent green pass
    from spider_qwen.evidence.transparency import (
        generate_signing_key,
        sign_tree_head,
        verify_signed_tree_head,
    )

    trusted_key = generate_signing_key()
    attacker_key = generate_signing_key()
    head = make_log(3).tree_head()
    legit = sign_tree_head(head, trusted_key)
    forged = sign_tree_head(head, attacker_key)  # internally consistent
    assert verify_signed_tree_head(legit, legit.public_key)
    assert not verify_signed_tree_head(forged, legit.public_key)
    assert not verify_signed_tree_head(legit, "")  # no anchor -> no trust


def test_ledger_persist_signs_tree_head_when_env_key_is_configured(tmp_path, monkeypatch):
    pytest.importorskip("cryptography")  # visible SKIP, never a silent green pass
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from spider_qwen.evidence.transparency import (
        generate_signing_key,
        verify_signed_tree_head,
        SignedTreeHead,
    )
    import json

    key = generate_signing_key()
    monkeypatch.setenv("SPIDER_QWEN_STH_SIGNING_KEY", key.hex())
    ledger = EvidenceLedger("run_signed", state_dir=tmp_path)
    ledger.record(source_tool="mock", url="https://example.com", snippet="s")
    path = ledger.persist()

    payload = json.loads(path.read_text(encoding="utf-8"))
    sth = SignedTreeHead.model_validate(payload["signed_tree_head"])
    assert sth.head.root_hash == payload["tree_head"]["root_hash"]
    # Trust-anchor semantics: derive the expected public key from the private
    # key we configured, independently of the (informational) embedded one.
    anchor = Ed25519PrivateKey.from_private_bytes(key).public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
    ).hex()
    assert verify_signed_tree_head(sth, anchor)
    assert not verify_signed_tree_head(sth, "00" * 32)


def test_repersist_without_env_key_keeps_published_signature(tmp_path, monkeypatch):
    pytest.importorskip("cryptography")
    from spider_qwen.evidence.transparency import generate_signing_key
    import json

    monkeypatch.setenv("SPIDER_QWEN_STH_SIGNING_KEY", generate_signing_key().hex())
    ledger = EvidenceLedger("run_keep_sig", state_dir=tmp_path)
    ledger.record(source_tool="mock", url="https://example.com", snippet="s")
    path = ledger.persist()
    signed = json.loads(path.read_text(encoding="utf-8"))["signed_tree_head"]

    # Key gone from the env, items unchanged: re-persisting (e.g. a resumed
    # verify run) must keep the still-valid published signature, not drop it.
    monkeypatch.delenv("SPIDER_QWEN_STH_SIGNING_KEY")
    reloaded = EvidenceLedger.load("run_keep_sig", tmp_path)
    reloaded.persist()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["signed_tree_head"] == signed


def test_malformed_sth_signing_key_fails_loud(monkeypatch):
    from spider_qwen.evidence.ledger import sth_signing_key_from_env

    monkeypatch.setenv("SPIDER_QWEN_STH_SIGNING_KEY", "not-hex")
    with pytest.raises(ValueError, match="encoded as hex"):
        sth_signing_key_from_env()
    monkeypatch.setenv("SPIDER_QWEN_STH_SIGNING_KEY", "abcd")
    with pytest.raises(ValueError, match="exactly 32 bytes"):
        sth_signing_key_from_env()
    monkeypatch.delenv("SPIDER_QWEN_STH_SIGNING_KEY")
    assert sth_signing_key_from_env() is None
