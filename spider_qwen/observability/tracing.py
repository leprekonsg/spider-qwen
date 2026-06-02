"""Per-step run tracing.

One TraceEvent per tool step (search, fetch, extract, rank, rfq) with timing
and status, so a run can be reconstructed end to end.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from typing import Any

from pydantic import BaseModel, Field


class TraceEvent(BaseModel):
    run_id: str
    mode: str
    step: str
    tool: str = ""
    input_count: int = 0
    output_count: int = 0
    latency_ms: int = 0
    status: str = "success"
    error: str | None = None
    # Structured step payload (e.g. CRAG verdict, DAG shape, cost breakdown).
    detail: dict[str, Any] | None = None
    timestamp: float = Field(default_factory=time.time)


class Tracer:
    def __init__(self, run_id: str, mode: str, state_dir: str | Path | None = None) -> None:
        self.run_id = run_id
        self.mode = mode
        self.events: list[TraceEvent] = []
        self._state_dir = Path(state_dir) if state_dir else None

    def record(
        self,
        *,
        step: str,
        tool: str = "",
        input_count: int = 0,
        output_count: int = 0,
        latency_ms: int = 0,
        status: str = "success",
        error: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.events.append(
            TraceEvent(
                run_id=self.run_id, mode=self.mode, step=step, tool=tool,
                input_count=input_count, output_count=output_count,
                latency_ms=latency_ms, status=status, error=error, detail=detail,
            )
        )

    def persist(self) -> Path | None:
        if not self._state_dir:
            return None
        target = self._state_dir / "traces" / f"{self.run_id}.trace.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps([e.model_dump() for e in self.events], indent=2), encoding="utf-8"
        )
        return target
