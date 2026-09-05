"""Offering-level product evaluation over explicitly labelled gold records."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .evaluate_service_mode import run_gold_set


_OFFERING_FIELDS = ("supplier_id", "offering_id", "vendor_name", "website", "product_name", "price", "currency", "unit", "moq", "quantity", "variant", "geography", "valid_from", "valid_until", "pricing_status")


def _matches_expected(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    """Match every explicit expected offering field; never infer omitted scope."""
    return all(actual.get(field) == expected[field] for field in _OFFERING_FIELDS if field in expected)


def _evaluate_offerings(case: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    expected = case.get("expected_offerings")
    if not isinstance(expected, list) or not expected:
        return {
            "status": "unavailable",
            "reason": "No independently labelled offering records in this case.",
            "expected_records": 0,
            "matched_records": 0,
        }
    if not all(
        isinstance(record, dict)
        and (record.get("supplier_id") or (record.get("vendor_name") and record.get("website")))
        and (record.get("offering_id") or record.get("product_name"))
        and record.get("price") is not None and record.get("currency") and record.get("unit")
        for record in expected
    ):
        return {
            "status": "unavailable",
            "reason": "Offering labels require supplier identity, item identity, price, currency and unit; partial labels cannot establish record correctness.",
            "expected_records": len(expected),
            "matched_records": 0,
        }
    records = row.get("product_records", [])
    matched = sum(any(_matches_expected(record, label) for record in records) for label in expected)
    return {
        "status": "available",
        "expected_records": len(expected),
        "matched_records": matched,
        "record_recall": round(matched / len(expected), 3),
    }


def run_product_gold_set(
    path: str | Path,
    offline: bool = True,
    *,
    profile: str | None = None,
) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = [case for case in data.get("cases", []) if case["expected_mode"] == "product_exact_price"]
    full = run_gold_set(path, offline=offline, profile=profile)
    by_id = {row["case_id"]: row for row in full["details"]}
    rows: list[dict[str, Any]] = []
    available_records = matched_records = 0
    for case in cases:
        base = by_id[case["case_id"]]
        offering = _evaluate_offerings(case, base)
        if offering["status"] == "available":
            available_records += offering["expected_records"]
            matched_records += offering["matched_records"]
        rows.append({
            "case_id": case["case_id"],
            "mode_match": base["mode_match"],
            "validated": base["validated"],
            "offering_evaluation": offering,
            # Kept as a data trace only; an aggregate status is not a price
            # correctness label for an offering.
            "pricing_statuses": sorted({record.get("pricing_status") for record in base["product_records"]}),
        })

    return {
        "cases": len(rows),
        "end_to_end_routing_accuracy": (
            round(sum(row["mode_match"] for row in rows) / len(rows), 3) if rows else None
        ),
        "offering_record_recall": (
            round(matched_records / available_records, 3) if available_records else None
        ),
        "offering_record_recall_status": (
            "available" if available_records else "unavailable: no structured offering labels in this gold set"
        ),
        "legacy_metric_aliases": {
            "mode_classification_accuracy": "end_to_end_routing_accuracy",
            "pricing_status_accuracy": "unavailable; aggregate status is not offering correctness",
        },
        # Do not manufacture a score by treating an expected status as an
        # offering label. Old consumers get null and an explicit reason.
        "mode_classification_accuracy": (
            round(sum(row["mode_match"] for row in rows) / len(rows), 3) if rows else None
        ),
        "pricing_status_accuracy": None,
        "evaluation_manifest": full["evaluation_manifest"],
        "details": rows,
    }
