"""Product-mode focused metrics over the gold set.

Reuses the shared harness and projects product-specific accuracy: did the
candidate land on a priced PricingStatus the case expected?
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from .evaluate_service_mode import _build_controller


def run_product_gold_set(path: str | Path, offline: bool = True) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = [c for c in data.get("cases", []) if c["expected_mode"] == "product_exact_price"]
    controller = _build_controller(offline)

    rows: list[dict[str, Any]] = []
    for case in cases:
        result = asyncio.run(controller.run(case["query"], mode="auto"))
        expected = set(case.get("expected_pricing_status", []))
        statuses = set(result.pricing_status_summary.keys())
        rows.append(
            {
                "case_id": case["case_id"],
                "mode_match": result.mode == "product_exact_price",
                "pricing_status_match": bool(expected & statuses) if expected else True,
                "validated": len(result.validated_candidates),
            }
        )

    n = len(rows) or 1
    return {
        "cases": len(rows),
        "mode_classification_accuracy": round(sum(r["mode_match"] for r in rows) / n, 3),
        "pricing_status_accuracy": round(sum(r["pricing_status_match"] for r in rows) / n, 3),
        "details": rows,
    }
