"""Append-only evidence ledger with per-run JSON persistence.

Every search result, fetched page, and extraction source is recorded here.
Downstream outputs carry EvidenceRef pointers (ledger_id) back into the ledger.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .dedupe import dedupe_items
from .models import EvidenceItem, EvidenceRef, SourceTool, sha256_hex

# run_ids are generated as "run_" + hex; reject anything else so a caller-supplied
# id can never traverse out of the state dir (e.g. "../../etc/passwd").
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_run_id(run_id: str) -> str:
    if not _SAFE_ID_RE.match(run_id or ""):
        raise ValueError(
            f"Invalid run_id '{run_id}': only letters, digits, '_' and '-' are allowed."
        )
    return run_id


def _chain_hash(content_digest: str, parent_hash: str) -> str:
    """Link a row to its parent: sha256(claim_bytes || parent_sha) (T-2.4)."""
    return sha256_hex(content_digest + parent_hash)


class ChainIssue(BaseModel):
    ledger_id: str
    reason: str


class ChainVerificationResult(BaseModel):
    run_id: str
    checked: int = 0
    issues: list[ChainIssue] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


class EvidenceLedger:
    """In-memory ledger for one run, optionally persisted to a JSON file."""

    def __init__(self, run_id: str, state_dir: str | Path | None = None, *,
                 reliability_priors: dict[str, float] | None = None) -> None:
        self.run_id = _validate_run_id(run_id)
        self._items: dict[str, EvidenceItem] = {}
        self._state_dir = Path(state_dir) if state_dir else None
        self._reliability_priors = reliability_priors
        self._chain_tip = ""  # chain_hash of the most recently appended row
        # True after annotate(): chain hashes no longer bind row content and
        # must be resealed (once, lazily) before the next chain reader.
        self._chain_stale = False
        # The tree_head this ledger was loaded with, if any: re-persisting an
        # unchanged ledger must keep the original commitment (timestamp), not
        # silently replace it with a recomputed one.
        self._loaded_tree_head: dict[str, Any] | None = None

    def record(
        self,
        *,
        source_tool: SourceTool,
        url: str,
        snippet: str,
        final_url: str | None = None,
        title: str | None = None,
        text: str | None = None,
        language: str | None = None,
        confidence: float = 0.0,
        metadata: dict[str, Any] | None = None,
    ) -> EvidenceRef:
        """Record one evidence item and return its reference."""
        # Imported lazily: governance/__init__ pulls in modes.contracts ->
        # evidence.models, which would cycle if imported at ledger module load.
        from ..governance.source_reliability import reliability_for

        _source_class, reliability = reliability_for(
            final_url or url, text=text or "", title=title or "",
            priors=self._reliability_priors,
        )
        item = EvidenceItem(
            source_tool=source_tool,
            url=url,
            final_url=final_url,
            title=title,
            snippet=snippet,
            snippet_hash=sha256_hex(snippet),
            text=text,
            text_hash=sha256_hex(text) if text else None,
            language=language,
            confidence=confidence,
            reliability=reliability,
            metadata=metadata or {},
        )
        self._seal_chain()  # the tip must bind final content before linking to it
        item.parent_hash = self._chain_tip
        item.chain_hash = _chain_hash(item.content_digest(), self._chain_tip)
        self._chain_tip = item.chain_hash
        self._items[item.ledger_id] = item
        return item.to_ref()

    def get(self, ledger_id: str) -> EvidenceItem | None:
        return self._items.get(ledger_id)

    def items(self) -> list[EvidenceItem]:
        return list(self._items.values())

    def refs(self) -> list[EvidenceRef]:
        return [item.to_ref() for item in self._items.values()]

    def deduped_items(self) -> list[EvidenceItem]:
        return dedupe_items(self.items())

    def transparency_log(self):
        """RFC 6962 Merkle log over the chain hashes (externally verifiable proofs)."""
        from .transparency import MerkleLog

        return MerkleLog.from_ledger(self)  # from_ledger seals a stale chain first

    def verify_chain(self) -> ChainVerificationResult:
        """Re-walk the Merkle chain; any tampered or mis-linked row is reported."""
        self._seal_chain()
        result = ChainVerificationResult(run_id=self.run_id)
        parent = ""
        for item in self._items.values():
            result.checked += 1
            expected = _chain_hash(item.content_digest(), parent)
            if item.parent_hash != parent:
                result.issues.append(ChainIssue(
                    ledger_id=item.ledger_id,
                    reason=f"parent_hash mismatch (expected {parent[:12] or 'genesis'})",
                ))
            elif item.chain_hash != expected:
                result.issues.append(ChainIssue(
                    ledger_id=item.ledger_id,
                    reason="chain_hash mismatch: row content was tampered",
                ))
            parent = item.chain_hash
        return result

    def annotate(self, ledger_id: str, updates: dict[str, Any]) -> None:
        """Annotate a row's metadata before the tree head is published.

        Verification writes verdict metadata onto claim rows after extraction;
        those annotations are part of the persisted evidence record, so the
        chain must bind the final row content. Mutating rows in place marks
        the chain stale; it is resealed once, lazily, by the next chain reader
        instead of per write.

        Rows covered by a published tree_head commitment (index < its
        tree_size) are refused: rewriting them would replace what an external
        party may already hold -- append a new row instead. Rows appended
        AFTER the commitment are fair game; resealing leaves the committed
        prefix's hashes unchanged (same content, same parents), so the prior
        commitment stays a consistent prefix of the next one.
        """
        item = self._items.get(ledger_id)
        if item is None:
            raise KeyError(f"ledger_id {ledger_id} not found in ledger {self.run_id}")
        if self._loaded_tree_head is not None:
            committed = int(self._loaded_tree_head.get("tree_size") or 0)
            index = list(self._items).index(ledger_id)
            if index < committed:
                raise ValueError(
                    f"Row {ledger_id} (index {index}) is covered by the published "
                    f"tree_head commitment of ledger {self.run_id} (tree_size "
                    f"{committed}); annotating it would rewrite the chain and "
                    "replace the commitment. Append a new evidence row instead."
                )
        item.metadata.update(updates)
        self._chain_stale = True

    def _seal_chain(self) -> None:
        """Re-link hashes once after annotate() calls. No-op when not stale."""
        if not self._chain_stale:
            return
        parent = ""
        for item in self._items.values():
            item.parent_hash = parent
            item.chain_hash = _chain_hash(item.content_digest(), parent)
            parent = item.chain_hash
        self._chain_tip = parent
        self._chain_stale = False

    def __len__(self) -> int:
        return len(self._items)

    def path(self) -> Path | None:
        if not self._state_dir:
            return None
        return self._state_dir / "evidence" / f"{self.run_id}.ledger.json"

    def persist(self) -> Path | None:
        target = self.path()
        if target is None:
            return None
        self._seal_chain()
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "run_id": self.run_id,
            "items": [item.model_dump() for item in self._items.values()],
        }
        if self._items:
            # Published commitment: external parties verify citation inclusion
            # proofs against this head without trusting the ledger file. If the
            # items are unchanged since load, keep the ORIGINAL commitment --
            # rewriting its timestamp would silently replace what an external
            # party may already have recorded.
            head = self.transparency_log().tree_head()
            prior = self._loaded_tree_head
            if prior and prior.get("root_hash") == head.root_hash \
                    and prior.get("tree_size") == head.tree_size:
                payload["tree_head"] = prior
            else:
                payload["tree_head"] = head.model_dump()
                self._loaded_tree_head = payload["tree_head"]
            signed = _signed_tree_head_from_env(payload["tree_head"])
            if signed is not None:
                payload["signed_tree_head"] = signed
        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return target

    @classmethod
    def load(cls, run_id: str, state_dir: str | Path) -> "EvidenceLedger":
        ledger = cls(run_id, state_dir)
        target = ledger.path()
        if target and target.exists():
            payload = json.loads(target.read_text(encoding="utf-8"))
            for raw in payload.get("items", []):
                item = EvidenceItem.model_validate(raw)
                ledger._items[item.ledger_id] = item
            if ledger._items:
                # Continue the chain from the last appended row.
                ledger._chain_tip = next(reversed(ledger._items.values())).chain_hash
            head = payload.get("tree_head")
            if head and ledger._items:
                # The persisted commitment must match the items it covers;
                # a mismatch means the file was edited after publication.
                recomputed = ledger.transparency_log().tree_head()
                if (head.get("run_id") != ledger.run_id
                        or head.get("tree_size") != recomputed.tree_size
                        or head.get("root_hash") != recomputed.root_hash):
                    raise ValueError(
                        f"Evidence ledger {target} does not match its published "
                        "tree_head commitment: the file was modified after the "
                        "commitment was written. Re-run `spider-qwen evidence "
                        "verify` or restore the file from backup."
                    )
            ledger._loaded_tree_head = head
        return ledger


def sth_signing_key_from_env() -> bytes | None:
    """Validate SPIDER_QWEN_STH_SIGNING_KEY; key bytes, or None when unset.

    Raises ValueError on a malformed key or missing crypto extra. The CLI calls
    this at startup so a bad key fails before any budget is spent, not at
    end-of-run persist; persist() calls it again at signing time.
    """
    private_key_hex = os.getenv("SPIDER_QWEN_STH_SIGNING_KEY", "").strip()
    if not private_key_hex:
        return None
    try:
        private_key = bytes.fromhex(private_key_hex)
    except ValueError as exc:
        raise ValueError(
            "SPIDER_QWEN_STH_SIGNING_KEY must be a 32-byte Ed25519 private key encoded as hex."
        ) from exc
    if len(private_key) != 32:
        raise ValueError(
            "SPIDER_QWEN_STH_SIGNING_KEY must decode to exactly 32 bytes."
        )
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519  # noqa: F401
    except ImportError as exc:
        raise ValueError(
            "SPIDER_QWEN_STH_SIGNING_KEY is set but signing is unavailable. "
            "Install with: pip install 'spider-qwen[crypto]'"
        ) from exc
    return private_key


def _signed_tree_head_from_env(head_payload: dict[str, Any]) -> dict[str, Any] | None:
    private_key = sth_signing_key_from_env()
    if private_key is None:
        return None
    from .transparency import TreeHead, sign_tree_head

    return sign_tree_head(TreeHead.model_validate(head_payload), private_key).model_dump(mode="json")
