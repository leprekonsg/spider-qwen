"""Evidence bundles: group the refs that justify a single candidate."""

from __future__ import annotations

from pydantic import BaseModel, Field

from .. import SCHEMA_VERSION
from .models import EvidenceRef


class EvidenceBundle(BaseModel):
    """All evidence supporting one candidate, keyed by the field it backs."""

    schema_version: str = SCHEMA_VERSION
    subject: str = ""
    by_field: dict[str, list[EvidenceRef]] = Field(default_factory=dict)

    def add(self, field: str, ref: EvidenceRef) -> None:
        self.by_field.setdefault(field, []).append(ref)

    def all_refs(self) -> list[EvidenceRef]:
        seen: set[str] = set()
        out: list[EvidenceRef] = []
        for refs in self.by_field.values():
            for ref in refs:
                if ref.ledger_id not in seen:
                    seen.add(ref.ledger_id)
                    out.append(ref)
        return out

    def has(self, field: str) -> bool:
        return bool(self.by_field.get(field))
