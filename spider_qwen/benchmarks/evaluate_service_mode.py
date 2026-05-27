"""Gold-set evaluation harness.

Runs every gold case through the Controller (offline mock providers by default)
and reports the spec's benchmark metrics. Used by `spider-qwen benchmark`.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

from ..agent.controller import Controller
from ..modes.classifier import ModeClassifier


def _build_controller(offline: bool) -> Controller:
    if offline:
        from ..tools.search_service import MockSearchProvider
        from ..tools.fetch_service import MockFetchProvider

        return Controller(
            search_provider=MockSearchProvider(),
            fetch_provider=MockFetchProvider(),
            state_dir=None,
            persist=False,
        )
    return Controller(persist=False)


async def _run_case(controller: Controller, case: dict[str, Any]) -> dict[str, Any]:
    result = await controller.run(case["query"], mode="auto")
    rfq_statuses = [d.get("status") for d in result.rfq_drafts]
    has_quote = any(
        c.get("quote_channel") for c in result.validated_candidates
    )
    return {
        "case_id": case["case_id"],
        "expected_mode": case["expected_mode"],
        "actual_mode": result.mode,
        "mode_match": result.mode == case["expected_mode"],
        "validated": len(result.validated_candidates),
        "has_quote_channel": has_quote,
        "rfq_complete": "complete" in rfq_statuses,
        "evidence_refs": len(result.evidence_refs),
        "stop_reason": result.stop_reason,
    }


def run_gold_set(path: str | Path, offline: bool = True) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = data.get("cases", [])
    controller = _build_controller(offline)

    started = time.time()
    rows = [asyncio.run(_run_case(controller, c)) for c in cases]
    elapsed = time.time() - started

    n = len(rows) or 1
    service_rows = [r for r in rows if r["expected_mode"] == "service_quote_required"]
    validated_rows = [r for r in rows if r["validated"] > 0]
    classifier = ModeClassifier()
    classification_correct = sum(
        1 for c in cases if classifier.classify(c["query"]).mode.value == c["expected_mode"]
    )

    summary = {
        "cases": len(rows),
        "mode_classification_accuracy": round(classification_correct / n, 3),
        "quote_channel_precision": round(
            sum(1 for r in service_rows if r["has_quote_channel"]) / (len(service_rows) or 1), 3
        ),
        "rfq_draft_completeness": round(
            sum(1 for r in service_rows if r["rfq_complete"]) / (len(service_rows) or 1), 3
        ),
        # Core invariant: every run that produced a validated candidate backed it
        # with evidence. (Cases that validate nothing don't count against coverage.)
        "evidence_coverage": round(
            sum(1 for r in validated_rows if r["evidence_refs"] > 0) / (len(validated_rows) or 1), 3
        ),
        "runtime_seconds": round(elapsed, 3),
        "offline": offline,
        "details": rows,
    }
    return summary
