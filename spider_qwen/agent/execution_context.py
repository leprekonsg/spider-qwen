"""Per-run execution context (working memory holder + shared services).

Bundles the run_id, query, mode, budget tracker, evidence ledger, tracer, and
the run's working memory so the controller can pass a single object around.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from .budget import BudgetTracker
    from ..evidence.ledger import EvidenceLedger
    from ..memory.working import WorkingMemory
    from ..observability.tracing import Tracer
    from ..modes.contracts import ProcurementMode


def new_run_id() -> str:
    return f"run_{uuid4().hex[:12]}"


@dataclass
class ExecutionContext:
    run_id: str
    query: str
    mode: "ProcurementMode"
    ledger: "EvidenceLedger"
    tracker: "BudgetTracker"
    working: "WorkingMemory"
    tracer: "Tracer | None" = None
    metadata: dict[str, Any] = field(default_factory=dict)
