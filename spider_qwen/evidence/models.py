"""Canonical evidence schemas.

An EvidenceItem is the full record stored in the ledger. An EvidenceRef is the
lightweight pointer embedded in downstream outputs (rankings, RFQ drafts,
semantic facts). Downstream code must reference ledger IDs, never raw URLs.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from .. import SCHEMA_VERSION

SourceTool = Literal[
    "tinyfish_search",
    "tinyfish_fetch",
    "qwen_web_extractor",
    "mcp_search",
    "semantic_memory",
    "mock",
]


def sha256_hex(text: str) -> str:
    """Stable hex digest used for snippet/text dedupe and evidence integrity."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_ledger_id() -> str:
    return f"ev_{uuid4().hex[:12]}"


class EvidenceRef(BaseModel):
    """Lightweight pointer to a ledger entry, safe to embed in outputs."""

    ledger_id: str
    url: str
    snippet_hash: str
    retrieved_at: str


class DisputedAlternative(BaseModel):
    """A competing value for a disputed fact, with its own evidence spans.

    Lets a contradicted (entity, property) retain *every* side's value and
    evidence rather than discarding the loser (T-2.3 contradiction policy).
    """

    schema_version: str = SCHEMA_VERSION
    value: str
    confidence: float = 0.0
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)


class EvidenceItem(BaseModel):
    """Full evidence record persisted in the ledger."""

    schema_version: str = SCHEMA_VERSION
    ledger_id: str = Field(default_factory=new_ledger_id)
    source_tool: SourceTool
    url: str
    final_url: str | None = None
    title: str | None = None
    retrieved_at: str = Field(default_factory=utc_now_iso)
    snippet: str = ""
    snippet_hash: str = ""
    text: str | None = None
    text_hash: str | None = None
    language: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.0
    # T-2.4: source-reliability prior (set by the ledger at record time) and the
    # Merkle hash chain linking this row to the one appended before it.
    reliability: float = 1.0
    parent_hash: str = ""
    chain_hash: str = ""

    def model_post_init(self, __context: Any) -> None:  # noqa: D401
        if not self.snippet_hash:
            object.__setattr__(self, "snippet_hash", sha256_hex(self.snippet))

    @property
    def effective_confidence(self) -> float:
        """Base confidence weighted by the source-reliability prior (T-2.4)."""
        return round(self.confidence * self.reliability, 6)

    def content_digest(self) -> str:
        """Stable digest over all content fields except the chain links.

        Tampering with any persisted field changes this digest, so re-walking
        the chain detects it.
        """
        payload = self.model_dump(mode="json", exclude={"chain_hash", "parent_hash"})
        return sha256_hex(json.dumps(payload, sort_keys=True, ensure_ascii=False))

    def to_ref(self) -> EvidenceRef:
        return EvidenceRef(
            ledger_id=self.ledger_id,
            url=self.final_url or self.url,
            snippet_hash=self.snippet_hash,
            retrieved_at=self.retrieved_at,
        )
