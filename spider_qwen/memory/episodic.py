"""Episodic memory: one summary record per completed run (JSON Lines)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from .. import SCHEMA_VERSION
from ..evidence.models import EvidenceRef, utc_now_iso


class EpisodicRecord(BaseModel):
    schema_version: str = SCHEMA_VERSION
    episode_id: str = Field(default_factory=lambda: f"ep_{uuid4().hex[:12]}")
    query: str
    mode: str
    summary: str
    created_at: str = Field(default_factory=utc_now_iso)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    outcome: Literal["success", "incomplete", "failed"] = "incomplete"


class EpisodicMemory:
    def __init__(self, state_dir: str | Path | None = None) -> None:
        self._state_dir = Path(state_dir) if state_dir else None

    def _path(self) -> Path | None:
        if not self._state_dir:
            return None
        return self._state_dir / "memory" / "episodic.jsonl"

    def append(self, record: EpisodicRecord) -> EpisodicRecord:
        path = self._path()
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record.model_dump()) + "\n")
        return record

    def all(self) -> list[EpisodicRecord]:
        path = self._path()
        if not path or not path.exists():
            return []
        return [EpisodicRecord.model_validate_json(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
