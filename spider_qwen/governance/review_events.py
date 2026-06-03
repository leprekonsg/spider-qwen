"""Persisted human-in-the-loop review events."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from .. import SCHEMA_VERSION
from ..evidence.models import utc_now_iso


ReviewStatus = Literal["pending", "approved", "rejected"]


class ReviewStatusTransitionError(Exception):
    pass


class PendingHumanReview(BaseModel):
    schema_version: str = SCHEMA_VERSION
    event_id: str = Field(default_factory=lambda: f"review_{uuid4().hex[:12]}")
    run_id: str
    reason: str
    proposed_action: str
    status: ReviewStatus = "pending"
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class ReviewStore:
    def __init__(self, state_dir: str | Path | None = None) -> None:
        self.state_dir = Path(state_dir) if state_dir else None

    def create(
        self,
        *,
        run_id: str,
        reason: str,
        proposed_action: str,
        detail: dict[str, Any] | None = None,
    ) -> PendingHumanReview:
        event = PendingHumanReview(
            run_id=run_id,
            reason=reason,
            proposed_action=proposed_action,
            detail=detail or {},
        )
        events = self.list()
        events.append(event)
        self._write(events)
        return event

    def list(self, *, status: ReviewStatus | None = None) -> list[PendingHumanReview]:
        events = self._read()
        if status is None:
            return events
        return [event for event in events if event.status == status]

    def approve(self, event_id: str) -> PendingHumanReview | None:
        return self._set_status(event_id, "approved")

    def reject(self, event_id: str) -> PendingHumanReview | None:
        return self._set_status(event_id, "rejected")

    def _set_status(self, event_id: str, status: ReviewStatus) -> PendingHumanReview | None:
        events = self._read()
        found: PendingHumanReview | None = None
        for event in events:
            if event.event_id == event_id:
                if event.status == status:
                    found = event
                    break
                if event.status != "pending":
                    raise ReviewStatusTransitionError(
                        f"Review event '{event_id}' is already {event.status}; cannot mark it {status}"
                    )
                event.status = status
                event.updated_at = utc_now_iso()
                found = event
                break
        if found is not None:
            self._write(events)
        return found

    def _path(self) -> Path | None:
        if self.state_dir is None:
            return None
        return self.state_dir / "reviews" / "pending_human_review.json"

    def _read(self) -> list[PendingHumanReview]:
        path = self._path()
        if path is None or not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError):
            # A corrupt store (e.g. an interrupted or interleaved write) must not
            # crash the run. Quarantine the bad file so the data is recoverable
            # and the next write starts clean rather than appending to garbage.
            path.replace(path.with_suffix(".corrupt"))
            return []
        return [PendingHumanReview.model_validate(item) for item in raw]

    def _write(self, events: list[PendingHumanReview]) -> None:
        path = self._path()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps([e.model_dump() for e in events], indent=2)
        # Write to a unique temp file then atomically replace, so a reader (or a
        # concurrent writer) never observes a half-written file. os.replace is
        # atomic on POSIX and Windows; the temp name is unique so concurrent
        # writers do not clobber each other's temp (last replace wins).
        tmp = path.with_name(f"{path.name}.{uuid4().hex[:8]}.tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)
