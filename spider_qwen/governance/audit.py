"""Append-only audit trail of agent decisions and actions.

Records what the agent did (e.g., rfq_draft_generated). v1 never records a
submission/send because none can occur.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class AuditEvent(BaseModel):
    run_id: str
    action: str
    detail: dict[str, Any] = Field(default_factory=dict)
    timestamp: float = Field(default_factory=time.time)


class AuditLog:
    # Actions that v1 must never emit.
    FORBIDDEN = frozenset({"rfq_submitted", "rfq_sent", "form_submitted", "email_sent"})

    def __init__(self, run_id: str, state_dir: str | Path | None = None) -> None:
        self.run_id = run_id
        self.events: list[AuditEvent] = []
        self._state_dir = Path(state_dir) if state_dir else None

    def record(self, action: str, **detail: Any) -> AuditEvent:
        if action in self.FORBIDDEN:
            raise ValueError(f"Action '{action}' is forbidden in v1 (RFQ draft only)")
        event = AuditEvent(run_id=self.run_id, action=action, detail=detail)
        self.events.append(event)
        return event

    def persist(self) -> Path | None:
        if not self._state_dir:
            return None
        target = self._state_dir / "audit" / f"{self.run_id}.audit.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps([e.model_dump() for e in self.events], indent=2), encoding="utf-8"
        )
        return target
