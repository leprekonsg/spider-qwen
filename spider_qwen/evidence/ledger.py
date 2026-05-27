"""Append-only evidence ledger with per-run JSON persistence.

Every search result, fetched page, and extraction source is recorded here.
Downstream outputs carry EvidenceRef pointers (ledger_id) back into the ledger.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .dedupe import dedupe_items
from .models import EvidenceItem, EvidenceRef, SourceTool, sha256_hex


class EvidenceLedger:
    """In-memory ledger for one run, optionally persisted to a JSON file."""

    def __init__(self, run_id: str, state_dir: str | Path | None = None) -> None:
        self.run_id = run_id
        self._items: dict[str, EvidenceItem] = {}
        self._state_dir = Path(state_dir) if state_dir else None

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
        item = EvidenceItem(
            source_tool=source_tool,
            url=url,
            final_url=final_url,
            title=title,
            snippet=snippet,
            snippet_hash=sha256_hex(snippet),
            text_hash=sha256_hex(text) if text else None,
            language=language,
            confidence=confidence,
            metadata=metadata or {},
        )
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
        return ledger
