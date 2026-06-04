"""T-4.1: A-MEM Zettelkasten note store.

Every evidence-backed fact becomes a small note tagged with Zettelkasten
attributes (entity / geo / supply-stage / risk-flag) and 1-3 "agentic" links to
the most-related existing notes (shared entity > supply-stage > geo > risk). The
backend is a plain JSON-Lines file -- a Mem0g-style graph (Qdrant/Neo4j) is a
deferred substrate, not needed at single-agent scale. Deterministic attribute
extraction; no network, no LLM in the default path.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

from .. import SCHEMA_VERSION
from ..evidence.models import EvidenceRef, utc_now_iso
from .semantic import SemanticFact

MAX_LINKS = 3

# Deterministic geo detection over the fact's entity/value text.
_GEO_KEYWORDS = {
    "Singapore": ("singapore", "sg "),
    "Malaysia": ("malaysia", "kuala lumpur", "pembekal"),
    "Indonesia": ("indonesia", "jakarta"),
    "Thailand": ("thailand", "bangkok"),
    "Vietnam": ("vietnam", "hanoi", "ho chi minh"),
    "Philippines": ("philippines", "manila"),
}
# field -> supply-stage attribute.
_STAGE_FOR_FIELD = {
    "quote_channel": "sourcing",
    "price": "pricing",
    "moq": "pricing",
    "lead_time": "pricing",
    "lifecycle_status": "lifecycle",
}
_RISK_TOKENS = ("nrnd", "eol", "obsolete", "last-time-buy", "ltb", "nla", "discontinued", "counterfeit")


class Note(BaseModel):
    schema_version: str = SCHEMA_VERSION
    note_id: str = Field(default_factory=lambda: f"note_{uuid4().hex[:12]}")
    entity: str
    geo: str | None = None
    supply_stage: str = "general"
    risk_flag: bool = False
    text: str
    source_fact_id: str | None = None
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now_iso)


def _detect_geo(text: str) -> str | None:
    low = f" {text.lower()} "
    for geo, tokens in _GEO_KEYWORDS.items():
        if any(tok in low for tok in tokens):
            return geo
    return None


def _supply_stage(field: str) -> str:
    if field.startswith("contact"):
        return "contact"
    return _STAGE_FOR_FIELD.get(field, "general")


def _is_risky(fact: SemanticFact) -> bool:
    if fact.status == "disputed":
        return True
    low = fact.value.lower()
    return any(tok in low for tok in _RISK_TOKENS)


class NoteStore:
    def __init__(self, state_dir: str | Path | None = None) -> None:
        self._state_dir = Path(state_dir) if state_dir else None
        self._notes: list[Note] = []
        self._load()

    def _path(self) -> Path | None:
        if not self._state_dir:
            return None
        return self._state_dir / "memory" / "notes.jsonl"

    def _load(self) -> None:
        path = self._path()
        if path and path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._notes.append(Note.model_validate_json(line))

    def all(self) -> list[Note]:
        return list(self._notes)

    def add_from_fact(self, fact: SemanticFact) -> Note:
        note = Note(
            entity=fact.entity_name,
            geo=_detect_geo(f"{fact.entity_name} {fact.value}"),
            supply_stage=_supply_stage(fact.field),
            risk_flag=_is_risky(fact),
            text=f"{fact.entity_name} {fact.field}: {fact.value}",
            source_fact_id=fact.fact_id,
            evidence_refs=list(fact.evidence_refs),
        )
        note.links = self._link(note)
        self._notes.append(note)
        self._persist(note)
        return note

    def _link(self, note: Note) -> list[str]:
        scored: list[tuple[int, str]] = []
        for other in self._notes:
            score = 0
            if other.entity.lower() == note.entity.lower():
                score += 3
            if other.supply_stage == note.supply_stage:
                score += 1
            if other.geo and other.geo == note.geo:
                score += 1
            if other.risk_flag and note.risk_flag:
                score += 1
            if score > 0:
                scored.append((score, other.note_id))
        scored.sort(key=lambda s: s[0], reverse=True)
        return [nid for _score, nid in scored[:MAX_LINKS]]

    def _persist(self, note: Note) -> None:
        path = self._path()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(note.model_dump()) + "\n")
