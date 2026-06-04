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
import hmac
import re

from pydantic import BaseModel, Field

from .. import SCHEMA_VERSION
from .models import utc_now_iso

_LEAF_PREFIX = b"\x00"
_NODE_PREFIX = b"\x01"
# Every digest handled by the verifiers must be a 64-char SHA-256 hex string.
# Proofs arrive from UNTRUSTED external input; a malformed element must make
# verification return False, never raise (bytes.fromhex would).
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _digest_or_none(value: str) -> str | None:
    """Normalize an untrusted hex digest; None when it is not one."""
    v = (value or "").strip().lower() if isinstance(value, str) else ""
    return v if _HEX64.match(v) else None

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

    schema_version: str = SCHEMA_VERSION
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


class RedactedLeafOpening(BaseModel):
    """Optional opening for a salted leaf commitment.

    The Merkle proof can publish only ``commitment`` as ``leaf_data``. A verifier
    who later receives this opening can confirm which original chain hash it
    represented without exposing every leaf in the public proof stream.
    """

    schema_version: str = SCHEMA_VERSION
    leaf_data: str
    salt: str
    commitment: str


def leaf_salt(master_salt: str, leaf_index: int) -> str:
    """Per-leaf salt derived from one master salt: HMAC-SHA256(master, index).

    One shared salt would make redaction one-shot -- the first opening reveals
    the salt for every leaf, letting anyone who later learns another row's
    chain hash confirm its membership. Deriving per leaf keeps unopened leaves
    blinded after any number of openings.
    """
    return hmac.new(master_salt.encode("utf-8"), str(leaf_index).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def redact_leaf_data(leaf_data: str, salt: str) -> str:
    """Commit to a leaf as SHA-256(salt || 0x00 || leaf_data).

    The 0x00 separator (never part of either string) makes the encoding
    unambiguous: without it, ("ab", "c...") and ("abc", "...") would commit
    identically.
    """
    return hashlib.sha256(
        salt.encode("utf-8") + b"\x00" + leaf_data.encode("utf-8")
    ).hexdigest()


def verify_redacted_leaf(opening: RedactedLeafOpening) -> bool:
    """Check an opening against its commitment. Inputs are untrusted: an
    opening whose leaf is not a chain-hash digest is invalid, never an error."""
    if _digest_or_none(opening.leaf_data) is None or not opening.salt:
        return False
    return redact_leaf_data(opening.leaf_data, opening.salt) == opening.commitment


class MerkleLog:
    """Append-only RFC 6962 Merkle tree over evidence chain hashes."""

    def __init__(self, run_id: str, leaves: list[str] | None = None) -> None:
        self.run_id = run_id
        self._leaves: list[str] = list(leaves or [])

    @classmethod
    def from_ledger(cls, ledger, *, redact_salt: str | None = None) -> "MerkleLog":
        """Build the log from an EvidenceLedger's per-item chain hashes.

        ``redact_salt`` is a master salt; each leaf is committed under its own
        derived ``leaf_salt(redact_salt, index)`` so opening one leaf does not
        unblind the rest.
        """
        seal = getattr(ledger, "_seal_chain", None)
        if callable(seal):
            seal()  # chain hashes must bind final row content before commitment
        leaves = [item.chain_hash for item in ledger.items()]
        if redact_salt is not None:
            leaves = [redact_leaf_data(leaf, leaf_salt(redact_salt, i))
                      for i, leaf in enumerate(leaves)]
        return cls(ledger.run_id, leaves)

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
                    leaf_data=self._leaves[index],
                    audit_path=self.inclusion_proof(index),
                    tree_head=self.tree_head(),
                )
        raise KeyError(f"ledger_id {ledger_id} not found in ledger {ledger.run_id}")


# --- pure verifiers (re-implementable by an external party) -----------------

def verify_inclusion(leaf_data: str, leaf_index: int, tree_size: int,
                     audit_path: list[str], root_hash: str) -> bool:
    """RFC 6962 inclusion-proof verification. Pure: needs no log access.

    All inputs are untrusted: a malformed proof (non-hex path element, bogus
    root) is a failed verification, never an exception.
    """
    if leaf_index < 0 or leaf_index >= tree_size:
        return False
    root = _digest_or_none(root_hash)
    path = [_digest_or_none(p) for p in audit_path]
    if root is None or any(p is None for p in path):
        return False
    fn, sn = leaf_index, tree_size - 1
    r = leaf_hash(leaf_data)
    for p in path:
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
    return sn == 0 and r == root


def verify_citation(proof: CitationProof) -> bool:
    return verify_inclusion(proof.leaf_data, proof.leaf_index,
                            proof.tree_head.tree_size, proof.audit_path,
                            proof.tree_head.root_hash)


def verify_consistency(first_size: int, second_size: int, first_root: str,
                       second_root: str, proof: list[str]) -> bool:
    """RFC 6962 consistency-proof verification: first tree is a prefix of second.

    All inputs are untrusted: a malformed proof is a failed verification,
    never an exception.
    """
    if first_size < 1 or first_size > second_size:
        return False
    first = _digest_or_none(first_root)
    second = _digest_or_none(second_root)
    elements = [_digest_or_none(p) for p in proof]
    if first is None or second is None or any(p is None for p in elements):
        return False
    if first_size == second_size:
        return not elements and first == second
    path = list(elements)
    # When first_size is a power of two, the first root is its own subtree hash.
    if first_size & (first_size - 1) == 0:
        path = [first] + path
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
    return sn == 0 and fr == first and sr == second


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


def verify_signed_tree_head(sth: SignedTreeHead, expected_public_key: str) -> bool:
    """Verify an STH against a trusted, out-of-band public key (hex).

    The key embedded in the STH is informational only: verifying against it
    would let any attacker who rewrites the ledger re-sign with a fresh key
    and self-validate. ``expected_public_key`` is the trust anchor the caller
    obtained independently (config, pinned file, prior exchange).
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:
        raise ImportError(_CRYPTO_HINT) from exc
    from cryptography.exceptions import InvalidSignature

    anchor = (expected_public_key or "").strip().lower()
    if not anchor or (sth.public_key or "").strip().lower() != anchor:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(anchor)).verify(
            bytes.fromhex(sth.signature), sth.signed_payload(),
        )
        return True
    except (InvalidSignature, ValueError):
        return False
