"""T-4.3: bi-temporal facts over the SQLite graph edge table.

A thin layer over ``GraphStore``: each observation is an append-only edge carrying
``valid_from`` (event_ts, when the fact became true) and ``recorded_at`` (ingest_ts,
when we observed it). Recording a newer fact for the same ``(src, dst, rel)`` closes
the prior open row's ``valid_to`` but never deletes it, so price/cert/lifecycle
history is fully retained. ``current`` is the open row with the max valid-from;
``as_of`` answers point-in-time queries. No Zep/Graphiti engine.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..evidence.models import utc_now_iso
from ..graph.store import GraphStore


class TemporalFact(BaseModel):
    src: str
    dst: str
    rel: str
    props: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.0
    reliability: float = 0.0
    evidence_claim_id: str
    valid_from: str | None = None
    valid_to: str | None = None
    recorded_at: str | None = None
    grade: str | None = None


class BiTemporalGraph:
    def __init__(self, store: GraphStore) -> None:
        self.store = store

    def record(
        self,
        src: str,
        dst: str,
        rel: str,
        *,
        confidence: float,
        reliability: float,
        evidence_claim_id: str,
        valid_from: str,
        recorded_at: str | None = None,
        grade: str | None = None,
        props: dict[str, Any] | None = None,
        supersede: bool = True,
    ) -> None:
        if supersede:
            # Close prior open rows that became true before this one (append-only).
            self.store.mark_superseded(src, dst, rel, valid_to=valid_from, before=valid_from)
        self.store.add_edge(
            src, dst, rel,
            confidence=confidence, reliability=reliability, evidence_claim_id=evidence_claim_id,
            event_ts=valid_from, ingest_ts=recorded_at or utc_now_iso(),
            valid_to=None, grade=grade, props=props,
        )

    def invalidate_conflicting(self, src: str, rel: str, *, keep_dst: str, valid_to: str) -> int:
        """Close open ``(src, rel)`` rows whose object contradicts ``keep_dst``.

        For functional relations (one true object at a time, e.g. ACQUIRED_BY),
        a newer fact with a different object closes the prior row's validity
        window at the new fact's valid-from. Rows are never deleted. Returns
        the number of rows closed.
        """
        return self.store.mark_conflicting_superseded(
            src, rel, keep_dst=keep_dst, valid_to=valid_to, before=valid_to,
        )

    def history(self, src: str, dst: str, rel: str) -> list[TemporalFact]:
        return [self._fact(r) for r in self.store.versions(src, dst, rel)]

    def current(self, src: str, dst: str, rel: str) -> TemporalFact | None:
        open_rows = [r for r in self.store.versions(src, dst, rel) if r["valid_to"] is None]
        if not open_rows:
            return None
        return self._fact(max(open_rows, key=lambda r: r["event_ts"] or ""))

    def as_of(self, src: str, dst: str, rel: str, ts: str) -> TemporalFact | None:
        valid = [
            r for r in self.store.versions(src, dst, rel)
            if (r["event_ts"] or "") <= ts and (r["valid_to"] is None or r["valid_to"] > ts)
        ]
        if not valid:
            return None
        return self._fact(max(valid, key=lambda r: r["event_ts"] or ""))

    @staticmethod
    def _fact(row: dict[str, Any]) -> TemporalFact:
        return TemporalFact(
            src=row["src"], dst=row["dst"], rel=row["rel"], props=row["props"],
            confidence=row["confidence"], reliability=row["reliability"],
            evidence_claim_id=row["evidence_claim_id"], valid_from=row["event_ts"],
            valid_to=row["valid_to"], recorded_at=row["ingest_ts"], grade=row["grade"],
        )
