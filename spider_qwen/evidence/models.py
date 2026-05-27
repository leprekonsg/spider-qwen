"""Canonical evidence schemas.

An EvidenceItem is the full record stored in the ledger. An EvidenceRef is the
lightweight pointer embedded in downstream outputs (rankings, RFQ drafts,
semantic facts). Downstream code must reference ledger IDs, never raw URLs.
"""

from __future__ import annotations

import hashlib
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

    def model_post_init(self, __context: Any) -> None:  # noqa: D401
        if not self.snippet_hash:
            object.__setattr__(self, "snippet_hash", sha256_hex(self.snippet))

    def to_ref(self) -> EvidenceRef:
        return EvidenceRef(
            ledger_id=self.ledger_id,
            url=self.final_url or self.url,
            snippet_hash=self.snippet_hash,
            retrieved_at=self.retrieved_at,
        )
