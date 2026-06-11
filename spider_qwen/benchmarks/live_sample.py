"""Hand-gradable live extraction sample.

The offline benchmark measures the mocks. This harness runs a small sample of
validation cases through the real pipeline and emits a template pairing every
extracted field with the page text it came from, so a human can grade it.
``score_live_sample`` then turns the graded file into measured live precision
and recall numbers -- the instrument that says whether live extraction recall
is the binding constraint.

Mirrors the conformal calibration workflow (template -> hand-grade -> check).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from ..agent.controller import Controller
from ..evidence.ledger import EvidenceLedger

SCHEMA_VERSION = "1.0"
EXCERPT_CHARS = 1500
_FIELD_KEYS = ("vendor_name", "quote_channel", "price", "currency",
               "pricing_status", "contacts")


def build_live_sample(
    set_path: str | Path,
    *,
    sample_size: int,
    state_dir: str | Path,
    offline: bool = False,
    controller: Controller | None = None,
) -> dict[str, Any]:
    """Run the first ``sample_size`` cases and emit the gradable template.

    First-N sampling is deterministic on purpose: re-running the harness
    grades the same cases. Evidence excerpts are loaded back from each run's
    persisted ledger, so what the grader reads is exactly what extraction saw.
    """
    cases = json.loads(Path(set_path).read_text(encoding="utf-8"))["cases"][:sample_size]
    controller = controller or Controller(state_dir=state_dir, offline=offline)

    # One event loop for all cases: live providers hold pooled connections
    # bound to the loop they were opened on, so per-case asyncio.run dies
    # with "Event loop is closed" from the second case onward.
    async def _run_all() -> list[dict[str, Any]]:
        return [await _sample_case(controller, case, state_dir) for case in cases]

    rows = asyncio.run(_run_all())
    return {
        "schema_version": SCHEMA_VERSION,
        "method": (
            "offline mock providers -- harness smoke test; this measures the "
            "mocks, not the web" if offline
            else "live providers; grade every candidate by hand"
        ),
        "offline": offline,
        "sample_size": len(rows),
        "cases": rows,
    }


async def _sample_case(
    controller: Controller, case: dict[str, Any], state_dir: str | Path,
) -> dict[str, Any]:
    result = await controller.run(case["query"], mode="auto")
    ledger = EvidenceLedger.load(result.run_id, state_dir)
    candidates = []
    for cand in result.validated_candidates:
        candidates.append({
            "fields": {k: cand[k] for k in _FIELD_KEYS if cand.get(k) is not None},
            "evidence": [
                _excerpt(ledger, ref) for ref in cand.get("evidence_refs", [])[:2]
            ],
            # Grade by hand: true if every extracted field above is supported
            # by the evidence excerpts, false otherwise. Left null, `check`
            # refuses the file loudly.
            "extraction_correct": None,
        })
    return {
        "case_id": case["case_id"],
        "query": case["query"],
        "expected_mode": case["expected_mode"],
        "actual_mode": result.mode,
        "mode_correct": result.mode == case["expected_mode"],
        "stop_reason": result.stop_reason,
        "fetch_outcomes": result.metrics.get("fetch_outcomes", {}),
        "run_id": result.run_id,
        "candidates": candidates,
        # Grade by hand: how many extractable vendors/contacts are visible in
        # the excerpts (or on the listed pages) that extraction MISSED. This
        # is the recall denominator's missing half.
        "missed_on_page": None,
    }


def _excerpt(ledger: EvidenceLedger, ref: dict[str, Any]) -> dict[str, Any]:
    item = ledger.get(ref.get("ledger_id", ""))
    if item is None:
        return {"ledger_id": ref.get("ledger_id"), "url": ref.get("url"),
                "text_excerpt": ""}
    return {
        "ledger_id": item.ledger_id,
        "url": item.final_url or item.url,
        "title": item.title,
        "text_excerpt": (item.text or item.snippet or "")[:EXCERPT_CHARS],
    }


def score_live_sample(payload: dict[str, Any]) -> dict[str, Any]:
    """Turn a fully hand-graded sample file into precision/recall numbers.

    Raises ValueError (with case ids) when any grading field is still null:
    a partially graded file must never produce a number that looks measured.
    """
    cases = payload.get("cases", [])
    ungraded: list[str] = []
    for case in cases:
        if not isinstance(case.get("missed_on_page"), int):
            ungraded.append(f"{case.get('case_id')}: missed_on_page")
        for i, cand in enumerate(case.get("candidates", [])):
            if not isinstance(cand.get("extraction_correct"), bool):
                ungraded.append(f"{case.get('case_id')}: candidates[{i}].extraction_correct")
    if ungraded:
        raise ValueError(
            f"{len(ungraded)} grading field(s) are still ungraded: "
            + "; ".join(ungraded[:8])
            + (" ..." if len(ungraded) > 8 else "")
            + ". Grade them by hand (extraction_correct: true/false, "
            "missed_on_page: integer), then re-run check."
        )

    graded = [c for case in cases for c in case.get("candidates", [])]
    correct = sum(1 for c in graded if c["extraction_correct"])
    missed = sum(case["missed_on_page"] for case in cases)
    mode_correct = sum(1 for case in cases if case.get("mode_correct"))
    per_mode: dict[str, dict[str, int]] = {}
    for case in cases:
        bucket = per_mode.setdefault(case["expected_mode"], {"cases": 0, "candidates": 0,
                                                             "correct": 0, "missed": 0})
        bucket["cases"] += 1
        bucket["candidates"] += len(case.get("candidates", []))
        bucket["correct"] += sum(1 for c in case.get("candidates", []) if c["extraction_correct"])
        bucket["missed"] += case["missed_on_page"]
    return {
        "offline": payload.get("offline", False),
        "cases": len(cases),
        "candidates_graded": len(graded),
        "mode_accuracy": round(mode_correct / (len(cases) or 1), 3),
        "extraction_precision": round(correct / (len(graded) or 1), 3),
        "extraction_recall": round(correct / ((correct + missed) or 1), 3),
        "missed_on_page_total": missed,
        "per_mode": per_mode,
    }
