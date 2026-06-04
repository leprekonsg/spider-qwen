"""Certificate-Transparency-style Merkle log over the evidence ledger (RFC 6962).

The linear T-2.4 chain proves order and tamper-evidence to anyone holding the
whole ledger. This module upgrades that to *externally verifiable* proofs: every
citation can ship an inclusion proof (audit path) against a signed tree head, so
a third party can confirm a claim was in the ledger at answer time without
trusting spider-qwen or downloading the full ledger.

Leaves are the per-item ``chain_hash`` values, so the Merkle tree and the linear
chain bind the same digests: forging either breaks both. Hashing follows RFC
6962 domain separation (0x00 leaf prefix, 0x01 node prefix); verification
functions are pure and dependency-free so an external verifier can re-implement
them from the RFC alone.

Ed25519 tree-head signing is optional and activates only when the
``cryptography`` package is installed (``pip install spider-qwen[crypto]``).
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, Field

from .. import SCHEMA_VERSION
from .models import utc_now_iso

_LEAF_PREFIX = b"\x00"
_NODE_PREFIX = b"\x01"

_CRYPTO_HINT = (
    "Ed25519 signing requires the 'cryptography' package. "
    "Install it with: pip install spider-qwen[crypto]"
)


def leaf_hash(data: str) -> str:
    """RFC 6962 leaf hash: SHA-256(0x00 || data)."""
    return hashlib.sha256(_LEAF_PREFIX + data.encode("utf-8")).hexdigest()


def _node(left: str, right: str) -> str:
    """RFC 6962 interior node: SHA-256(0x01 || left || right) over raw digests."""
    return hashlib.sha256(
        _NODE_PREFIX + bytes.fromhex(left) + bytes.fromhex(right)
    ).hexdigest()


def _largest_power_of_two_below(n: int) -> int:
    """Largest power of two strictly less than n (n >= 2)."""
    k = 1
    while k * 2 < n:
        k *= 2
    return k


class TreeHead(BaseModel):
    """Unsigned tree head: the log's published commitment at a point in time."""

    schema_version: str = SCHEMA_VERSION
    run_id: str
    tree_size: int
    root_hash: str
    timestamp: str = Field(default_factory=utc_now_iso)


class SignedTreeHead(BaseModel):
    """Ed25519-signed tree head (RFC 6962 STH analog)."""

    head: TreeHead
    signature: str  # hex
    public_key: str  # hex, raw Ed25519 public bytes
    algorithm: str = "ed25519"

    def signed_payload(self) -> bytes:
        return _sth_payload(self.head)


class CitationProof(BaseModel):
    """Everything an external verifier needs to confirm one citation.

    Verification needs no ledger access: recompute ``leaf_hash(leaf_data)``,
    walk ``audit_path``, compare against ``tree_head.root_hash``.
    """

    schema_version: str = SCHEMA_VERSION
    ledger_id: str
    leaf_index: int
    leaf_data: str  # the evidence item's chain_hash
    audit_path: list[str] = Field(default_factory=list)
    tree_head: TreeHead


class MerkleLog:
    """Append-only RFC 6962 Merkle tree over evidence chain hashes."""

    def __init__(self, run_id: str, leaves: list[str] | None = None) -> None:
        self.run_id = run_id
        self._leaves: list[str] = list(leaves or [])

    @classmethod
    def from_ledger(cls, ledger) -> "MerkleLog":
        """Build the log from an EvidenceLedger's per-item chain hashes."""
        return cls(ledger.run_id, [item.chain_hash for item in ledger.items()])

    def append(self, leaf_data: str) -> int:
        self._leaves.append(leaf_data)
        return len(self._leaves) - 1

    def __len__(self) -> int:
        return len(self._leaves)

    # --- roots --------------------------------------------------------------

    def root_hash(self, tree_size: int | None = None) -> str:
        n = len(self._leaves) if tree_size is None else tree_size
        if n < 0 or n > len(self._leaves):
            raise ValueError(f"tree_size {n} out of range (log has {len(self._leaves)} leaves)")
        return self._mth(0, n)

    def _mth(self, start: int, end: int) -> str:
        """Merkle tree hash over leaves[start:end] (RFC 6962 section 2.1)."""
        n = end - start
        if n == 0:
            return hashlib.sha256(b"").hexdigest()
        if n == 1:
            return leaf_hash(self._leaves[start])
        k = _largest_power_of_two_below(n)
        return _node(self._mth(start, start + k), self._mth(start + k, end))

    def tree_head(self) -> TreeHead:
        return TreeHead(run_id=self.run_id, tree_size=len(self._leaves),
                        root_hash=self.root_hash())

    # --- proofs ---------------------------------------------------------------

    def inclusion_proof(self, leaf_index: int, tree_size: int | None = None) -> list[str]:
        """Audit path for leaves[leaf_index] in the tree of ``tree_size`` leaves."""
        n = len(self._leaves) if tree_size is None else tree_size
        if not 0 <= leaf_index < n or n > len(self._leaves):
            raise ValueError(f"leaf_index {leaf_index} out of range for tree_size {n}")
        return self._path(leaf_index, 0, n)

    def _path(self, m: int, start: int, end: int) -> list[str]:
        n = end - start
        if n <= 1:
            return []
        k = _largest_power_of_two_below(n)
        if m < k:
            return self._path(m, start, start + k) + [self._mth(start + k, end)]
        return self._path(m - k, start + k, end) + [self._mth(start, start + k)]

    def consistency_proof(self, first_size: int, second_size: int | None = None) -> list[str]:
        """Prove the tree of ``first_size`` leaves is a prefix of ``second_size``."""
        n = len(self._leaves) if second_size is None else second_size
        if not 1 <= first_size <= n or n > len(self._leaves):
            raise ValueError(f"invalid consistency range {first_size}..{n}")
        if first_size == n:
            return []
        return self._subproof(first_size, 0, n, True)

    def _subproof(self, m: int, start: int, end: int, complete: bool) -> list[str]:
        n = end - start
        if m == n:
            return [] if complete else [self._mth(start, end)]
        k = _largest_power_of_two_below(n)
        if m <= k:
            return self._subproof(m, start, start + k, complete) + [self._mth(start + k, end)]
        return self._subproof(m - k, start + k, end, False) + [self._mth(start, start + k)]

    def citation_proof(self, ledger, ledger_id: str) -> CitationProof:
        """Bundle the inclusion proof for one evidence item's citation."""
        items = ledger.items()
        for index, item in enumerate(items):
            if item.ledger_id == ledger_id:
                return CitationProof(
                    ledger_id=ledger_id,
                    leaf_index=index,
                    leaf_data=item.chain_hash,
                    audit_path=self.inclusion_proof(index),
                    tree_head=self.tree_head(),
                )
        raise KeyError(f"ledger_id {ledger_id} not found in ledger {ledger.run_id}")


# --- pure verifiers (re-implementable by an external party) -----------------

def verify_inclusion(leaf_data: str, leaf_index: int, tree_size: int,
                     audit_path: list[str], root_hash: str) -> bool:
    """RFC 6962 inclusion-proof verification. Pure: needs no log access."""
    if leaf_index < 0 or leaf_index >= tree_size:
        return False
    fn, sn = leaf_index, tree_size - 1
    r = leaf_hash(leaf_data)
    for p in audit_path:
        if sn == 0:
            return False
        if fn % 2 == 1 or fn == sn:
            r = _node(p, r)
            if fn % 2 == 0:
                while fn % 2 == 0 and fn != 0:
                    fn >>= 1
                    sn >>= 1
        else:
            r = _node(r, p)
        fn >>= 1
        sn >>= 1
    return sn == 0 and r == root_hash


def verify_citation(proof: CitationProof) -> bool:
    return verify_inclusion(proof.leaf_data, proof.leaf_index,
                            proof.tree_head.tree_size, proof.audit_path,
                            proof.tree_head.root_hash)


def verify_consistency(first_size: int, second_size: int, first_root: str,
                       second_root: str, proof: list[str]) -> bool:
    """RFC 6962 consistency-proof verification: first tree is a prefix of second."""
    if first_size < 1 or first_size > second_size:
        return False
    if first_size == second_size:
        return not proof and first_root == second_root
    path = list(proof)
    # When first_size is a power of two, the first root is its own subtree hash.
    if first_size & (first_size - 1) == 0:
        path = [first_root] + path
    if not path:
        return False
    fn, sn = first_size - 1, second_size - 1
    while fn % 2 == 1:
        fn >>= 1
        sn >>= 1
    fr = sr = path[0]
    for c in path[1:]:
        if sn == 0:
            return False
        if fn % 2 == 1 or fn == sn:
            fr = _node(c, fr)
            sr = _node(c, sr)
            if fn % 2 == 0:
                while fn % 2 == 0 and fn != 0:
                    fn >>= 1
                    sn >>= 1
        else:
            sr = _node(sr, c)
        fn >>= 1
        sn >>= 1
    return sn == 0 and fr == first_root and sr == second_root


# --- optional Ed25519 signing (spider-qwen[crypto]) --------------------------

def _sth_payload(head: TreeHead) -> bytes:
    return f"{head.run_id}|{head.tree_size}|{head.root_hash}|{head.timestamp}".encode("utf-8")


def generate_signing_key() -> bytes:
    """Return raw Ed25519 private key bytes (32 bytes). Keep out of git."""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as exc:  # pragma: no cover - exercised only without extra
        raise ImportError(_CRYPTO_HINT) from exc
    from cryptography.hazmat.primitives import serialization

    key = Ed25519PrivateKey.generate()
    return key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )


def sign_tree_head(head: TreeHead, private_key_bytes: bytes) -> SignedTreeHead:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError as exc:
        raise ImportError(_CRYPTO_HINT) from exc
    from cryptography.hazmat.primitives import serialization

    key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    public = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw,
    )
    return SignedTreeHead(
        head=head,
        signature=key.sign(_sth_payload(head)).hex(),
        public_key=public.hex(),
    )


def verify_signed_tree_head(sth: SignedTreeHead) -> bool:
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise ImportError(_CRYPTO_HINT) from exc
    from cryptography.exceptions import InvalidSignature

    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(sth.public_key)).verify(
            bytes.fromhex(sth.signature), sth.signed_payload(),
        )
        return True
    except (InvalidSignature, ValueError):
        return False
