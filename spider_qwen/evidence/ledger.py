"""Append-only evidence ledger with per-run JSON persistence.

Every search result, fetched page, and extraction source is recorded here.
Downstream outputs carry EvidenceRef pointers (ledger_id) back into the ledger.
"""

from __future__ import annotations

import json
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

        return MerkleLog.from_ledger(self)

    def verify_chain(self) -> ChainVerificationResult:
        """Re-walk the Merkle chain; any tampered or mis-linked row is reported."""
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
