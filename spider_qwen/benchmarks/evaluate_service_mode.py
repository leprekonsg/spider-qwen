"""Gold-set evaluation of the configured controller path.

The gold set is a deterministic regression instrument. It does not contain
human procurement-outcome labels, so it reports routing, must-find contracts,
and evidence presence rather than supplier precision.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
import time
from pathlib import Path
from typing import Any

from ..api.factory import build_controller
from ..application.profiles import OperatorProfile, get_profile
from ..modes.classifier import ModeClassifier
from .manifest import evaluation_manifest


def _profile_for(offline: bool, profile: str | None) -> OperatorProfile:
    resolved = get_profile(profile or ("offline_demo" if offline else "live_research"))
    if resolved.offline != offline:
        raise ValueError(
            f"Profile '{resolved.name}' has offline={resolved.offline}, but evaluator offline={offline}."
        )
    return resolved


def _build_controller(
    offline: bool,
    *,
    profile: str | None = None,
    state_dir: str | None = None,
):
    """Use the same controller factory and profile settings as RunService."""
    resolved = _profile_for(offline, profile)
    effective = resolved.effective_config()
    return build_controller(
        offline=resolved.offline,
        state_dir=state_dir,
        qwen_json=resolved.qwen_json,
        verify=resolved.verify,
        require_review=resolved.require_review,
        rfq_grade_floor=resolved.rfq_grade_floor,
        expected_config_fingerprint=effective["config_fingerprint"],
    )


def _must_find_observations(result: Any) -> dict[str, Any]:
    candidates = result.validated_candidates
    return {
        "quote_channel": any(candidate.get("quote_channel") for candidate in candidates),
        "rfq_draft_status": {draft.get("status") for draft in result.rfq_drafts},
        "contact": any(candidate.get("contacts") for candidate in candidates),
        # The revalidation and electronics contracts have no explicit result
        # fields yet. Marking them unavailable prevents a mode match from being
        # mistaken for proof that the required outcome occurred.
        "revalidation": None,
        "substitute": None,
        "long_tail_source": None,
        "risk_signal": None,
    }


def _evaluate_must_find(case: dict[str, Any], result: Any) -> dict[str, dict[str, Any]]:
    observations = _must_find_observations(result)
    outcomes: dict[str, dict[str, Any]] = {}
    for key, expected in (case.get("must_find") or {}).items():
        # Fixture difficulty tags describe the case; they are not output claims.
        if key == "adversarial":
            continue
        actual = observations.get(key, "__unknown__")
        if actual == "__unknown__":
            outcomes[key] = {"expected": expected, "status": "unavailable",
                             "reason": "No evaluator predicate is defined for this expectation."}
        elif actual is None:
            outcomes[key] = {"expected": expected, "status": "unavailable",
                             "reason": "The controller does not expose this outcome yet."}
        elif key == "rfq_draft_status":
            outcomes[key] = {"expected": expected, "actual": sorted(actual),
                             "status": "passed" if expected in actual else "failed"}
        else:
            outcomes[key] = {"expected": expected, "actual": actual,
                             "status": "passed" if actual == expected else "failed"}
    return outcomes


async def _run_case(controller: Any, case: dict[str, Any]) -> dict[str, Any]:
    run_kwargs: dict[str, Any] = {"mode": "auto"}
    for key in ("requirements", "requirements_confirmed", "supplier_sources"):
        if key in case:
            run_kwargs[key] = case[key]
    result = await controller.run(case["query"], **run_kwargs)
    rfq_statuses = [draft.get("status") for draft in result.rfq_drafts]
    procurement_request = getattr(result, "procurement_request", None) or {}
    requirements = procurement_request.get("requirements", case.get("requirements", []))
    required_claims = [item for item in requirements if item.get("kind") == "mandatory"]
    requirement_ids = {item.get("requirement_id") for item in required_claims}
    evaluation_candidates: list[dict[str, Any]] = []
    seen_candidates: set[tuple[str, str, str]] = set()
    for candidate in list(result.validated_candidates) + list(result.withheld_candidates):
        key = (
            str(candidate.get("supplier_id") or ""), str(candidate.get("offering_id") or ""),
            str(candidate.get("vendor_name") or ""),
        )
        if key not in seen_candidates:
            seen_candidates.add(key)
            evaluation_candidates.append(candidate)
    assessments_by_claim: dict[tuple[str, str, str], dict[str, Any]] = {}
    for candidate in evaluation_candidates:
        candidate_key = str(candidate.get("offering_id") or candidate.get("supplier_id") or candidate.get("vendor_name") or "")
        for assessment in candidate.get("requirement_assessments", []):
            requirement_id = assessment.get("requirement_id")
            if requirement_id in requirement_ids:
                key = (candidate_key, str(requirement_id), str(assessment.get("scope") or ""))
                assessments_by_claim[key] = assessment
    assessments = list(assessments_by_claim.values())
    must_find = _evaluate_must_find(case, result)
    return {
        "case_id": case["case_id"],
        "expected_mode": case["expected_mode"],
        "actual_mode": result.mode,
        "mode_match": result.mode == case["expected_mode"],
        "validated": len(result.validated_candidates),
        "has_quote_channel": any(candidate.get("quote_channel") for candidate in result.validated_candidates),
        "rfq_complete": "complete" in rfq_statuses,
        "evidence_refs": len(result.evidence_refs),
        "candidate_evidence_present": all(
            candidate.get("evidence_refs") for candidate in evaluation_candidates
        ) if evaluation_candidates else None,
        "required_claims": len(required_claims),
        "required_claim_expected_assessments": len(required_claims) * len(evaluation_candidates),
        "required_claim_assessed": len(assessments),
        "required_claim_supported": sum(item.get("status") == "supported" for item in assessments),
        "required_claim_contradicted": sum(item.get("status") == "contradicted" for item in assessments),
        "required_claim_unresolved": sum(item.get("status") == "not_found" for item in assessments),
        "evaluation_candidate_count": len(evaluation_candidates),
        "withheld": len(result.withheld_candidates),
        "product_records": [
            {
                key: candidate.get(key)
                for key in (
                    "supplier_id", "offering_id", "vendor_name", "product_name", "price",
                    "currency", "unit", "moq", "pricing_status", "website", "quantity",
                    "variant", "geography", "valid_from", "valid_until",
                )
            }
            for candidate in result.validated_candidates
            if "pricing_status" in candidate
        ],
        "must_find": must_find,
        "stop_reason": result.stop_reason,
    }


async def _run_all(controller: Any, cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # A controller can own pooled live clients. Keep every case on one loop.
    return [await _run_case(controller, case) for case in cases]


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 3) if denominator else None


def _must_find_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    outcomes = [item for row in rows for item in row["must_find"].values()]
    passed = sum(item["status"] == "passed" for item in outcomes)
    failed = sum(item["status"] == "failed" for item in outcomes)
    available = passed + failed
    return {
        "expectations": len(outcomes),
        "available": available,
        "passed": passed,
        "failed": failed,
        "unavailable": sum(item["status"] == "unavailable" for item in outcomes),
        "pass_rate": _rate(passed, available),
    }


def run_gold_set(
    path: str | Path,
    offline: bool = True,
    *,
    profile: str | None = None,
) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = data.get("cases", [])
    resolved_profile = _profile_for(offline, profile)

    # Empty transient state makes the memory/cache condition in the manifest
    # true instead of merely descriptive.
    with tempfile.TemporaryDirectory(prefix="spider-qwen-benchmark-") as state_dir:
        controller = _build_controller(offline, profile=resolved_profile.name, state_dir=state_dir)
        started = time.time()
        rows = asyncio.run(_run_all(controller, cases))
        elapsed = time.time() - started

    n = len(rows)
    service_rows = [row for row in rows if row["expected_mode"] == "service_quote_required"]
    emitted_rows = [row for row in rows if row["validated"] > 0]
    classifier = ModeClassifier()
    baseline_matches = {
        case["case_id"]: classifier.classify(case["query"]).mode.value == case["expected_mode"]
        for case in cases
    }
    per_mode: dict[str, dict[str, Any]] = {}
    for mode in sorted({case["expected_mode"] for case in cases}):
        mode_cases = [case for case in cases if case["expected_mode"] == mode]
        mode_rows = [row for row in rows if row["expected_mode"] == mode]
        per_mode[mode] = {
            "cases": len(mode_cases),
            "end_to_end_routing_accuracy": _rate(
                sum(row["mode_match"] for row in mode_rows), len(mode_rows)
            ),
            "deterministic_classifier_baseline_accuracy": _rate(
                sum(baseline_matches[case["case_id"]] for case in mode_cases), len(mode_cases)
            ),
        }

    expected_required_claim_assessments = sum(row["required_claim_expected_assessments"] for row in rows)
    assessed_required_claims = sum(row["required_claim_assessed"] for row in rows)
    summary = {
        "cases": n,
        "end_to_end_routing_accuracy": _rate(sum(row["mode_match"] for row in rows), n),
        "deterministic_classifier_baseline_accuracy": _rate(sum(baseline_matches.values()), n),
        "quote_channel_yield": _rate(
            sum(row["has_quote_channel"] for row in service_rows), len(service_rows)
        ),
        "rfq_draft_yield": _rate(sum(row["rfq_complete"] for row in service_rows), len(service_rows)),
        "candidate_evidence_presence_rate": _rate(
            sum(row["candidate_evidence_present"] is True for row in emitted_rows), len(emitted_rows)
        ),
        "required_claim_assessment_coverage": _rate(
            assessed_required_claims, expected_required_claim_assessments
        ),
        "required_claim_support_rate": _rate(
            sum(row["required_claim_supported"] for row in rows), assessed_required_claims
        ),
        "required_claim_contradicted_rate": _rate(
            sum(row["required_claim_contradicted"] for row in rows), assessed_required_claims
        ),
        "required_claim_unresolved_rate": _rate(
            sum(row["required_claim_unresolved"] for row in rows), assessed_required_claims
        ),
        "required_claim_metrics_status": (
            "available" if expected_required_claim_assessments
            else "unavailable: no typed mandatory requirements with evaluated candidates in this gold set"
        ),
        "required_claim_empty_runs": sum(
            row["required_claims"] > 0 and row["evaluation_candidate_count"] == 0 for row in rows
        ),
        "must_find": _must_find_summary(rows),
        "legacy_metric_aliases": {
            "mode_classification_accuracy": "end_to_end_routing_accuracy",
            "rfq_draft_completeness": "rfq_draft_yield",
            "evidence_coverage": "candidate_evidence_presence_rate",
            "quote_channel_precision": "quote_channel_yield",
        },
        "runtime_seconds": round(elapsed, 3),
        "offline": offline,
        "per_mode": per_mode,
        "method": data.get("method", "offline deterministic gold set" if offline else "live providers"),
        "evaluation_manifest": evaluation_manifest(
            resolved_profile,
            memory_condition="cold_start_then_shared_state",
            page_cache_condition="cold_start_then_shared_state",
        ),
        "details": rows,
    }
    # Compatibility values remain, but the mapping above makes their actual
    # denominator and deprecation explicit to old readers.
    summary["mode_classification_accuracy"] = summary["end_to_end_routing_accuracy"]
    summary["quote_channel_precision"] = summary["quote_channel_yield"]
    summary["rfq_draft_completeness"] = summary["rfq_draft_yield"]
    summary["evidence_coverage"] = summary["candidate_evidence_presence_rate"]
    return summary
