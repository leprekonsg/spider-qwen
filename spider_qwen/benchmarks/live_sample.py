"""Hand-gradable live extraction and procurement-outcome samples."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from ..agent.controller import Controller
from ..api.factory import build_controller
from ..application.profiles import get_profile
from ..evidence.ledger import EvidenceLedger
from .manifest import evaluation_manifest

SCHEMA_VERSION = "2.0"
EXCERPT_CHARS = 4000
_FIELD_KEYS = ("supplier_id", "offering_id", "vendor_name", "quote_channel", "price", "currency",
               "unit", "moq", "pricing_status", "contacts", "requirement_assessments", "qualification")


def _sample_cases(cases: list[dict[str, Any]], sample_size: int, sampling_seed: str) -> list[dict[str, Any]]:
    """Deterministic spread sampling, stable under input-file reordering."""
    if sample_size < 0:
        raise ValueError("sample_size must be non-negative.")
    ranked = sorted(
        cases,
        key=lambda case: hashlib.sha256(
            f"{sampling_seed}:{case.get('case_id', '')}".encode("utf-8")
        ).hexdigest(),
    )
    return ranked[:sample_size]


def build_live_sample(
    set_path: str | Path,
    *,
    sample_size: int,
    state_dir: str | Path,
    offline: bool = False,
    controller: Controller | None = None,
    profile: str | None = None,
    sampling_seed: str = "spider-qwen-live-sample-v1",
) -> dict[str, Any]:
    """Run a deterministic spread sample on one event loop and emit label slots."""
    all_cases = json.loads(Path(set_path).read_text(encoding="utf-8"))["cases"]
    cases = _sample_cases(all_cases, sample_size, sampling_seed)
    resolved_profile = get_profile(profile or ("offline_demo" if offline else "live_research"))
    if resolved_profile.offline != offline:
        raise ValueError(
            f"Profile '{resolved_profile.name}' has offline={resolved_profile.offline}, but offline={offline}."
        )
    effective = resolved_profile.effective_config()
    state_path = Path(state_dir)
    state_preexisted = state_path.exists() and any(state_path.iterdir())
    supplied_controller = controller is not None
    controller = controller or build_controller(
        state_dir=str(state_dir),
        offline=resolved_profile.offline,
        qwen_json=resolved_profile.qwen_json,
        verify=resolved_profile.verify,
        require_review=resolved_profile.require_review,
        rfq_grade_floor=resolved_profile.rfq_grade_floor,
        expected_config_fingerprint=effective["config_fingerprint"],
    )

    async def _run_all() -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for case in cases:
            started = time.perf_counter()
            try:
                row = await _sample_case(controller, case, state_dir)
            except Exception as exc:
                # A failed attempt remains in the denominator. It must be
                # hand-labelled for task completion/reference-pool outcomes.
                row = _failed_case(case, exc)
            row.setdefault("operational_metrics", {}).setdefault(
                "elapsed_seconds", round(time.perf_counter() - started, 3)
            )
            rows.append(row)
        return rows

    rows = asyncio.run(_run_all())
    return {
        "schema_version": SCHEMA_VERSION,
        "method": (
            "offline mock providers -- harness smoke test; this measures the mocks, not the web" if offline
            else "live providers; hand-grade extraction and procurement outcomes"
        ),
        "offline": offline,
        "sample_size": len(rows),
        "sampling": {"method": "sha256_case_id_order", "seed": sampling_seed,
                     "population_cases": len(all_cases)},
        "evaluation_manifest": evaluation_manifest(
            resolved_profile,
            memory_condition=(
                "caller_supplied_controller_or_state" if supplied_controller or state_preexisted
                else "cold_start_then_shared_state"
            ),
            page_cache_condition=(
                "caller_supplied_controller_or_state" if supplied_controller or state_preexisted
                else "cold_start_then_shared_state"
            ),
        ),
        "cases": rows,
    }


async def _sample_case(
    controller: Controller, case: dict[str, Any], state_dir: str | Path,
) -> dict[str, Any]:
    run_kwargs: dict[str, Any] = {"mode": "auto"}
    for key in ("requirements", "requirements_confirmed", "supplier_sources"):
        if key in case:
            run_kwargs[key] = case[key]
    result = await controller.run(case["query"], **run_kwargs)
    ledger = EvidenceLedger.load(result.run_id, state_dir)
    candidates = [_candidate_bundle(candidate, ledger) for candidate in result.validated_candidates]
    requirements = (getattr(result, "procurement_request", None) or {}).get("requirements", [])
    return {
        "case_id": case["case_id"],
        "query": case["query"],
        "expected_mode": case["expected_mode"],
        "actual_mode": result.mode,
        "mode_correct": result.mode == case["expected_mode"],
        "stop_reason": result.stop_reason,
        "fetch_outcomes": result.metrics.get("fetch_outcomes", {}),
        "run_id": result.run_id,
        "requirements": requirements,
        "requirements_confirmed": (getattr(result, "procurement_request", None) or {}).get(
            "requirements_confirmed"
        ),
        "supplier_sources": (getattr(result, "procurement_request", None) or {}).get(
            "supplier_sources", {}
        ),
        "candidates": candidates,
        "withheld_candidates": [
            _candidate_bundle(candidate, ledger) for candidate in result.withheld_candidates
        ],
        "rejected_observations": {
            "pages_rejected": result.metrics.get("pages_rejected", 0),
            "candidates_blocked_unverified": result.metrics.get("candidates_blocked_unverified", 0),
            "fetch_outcomes": result.metrics.get("fetch_outcomes", {}),
        },
        "operational_metrics": {
            "cost": result.metrics.get("cost"),
            "latency_seconds": result.metrics.get("latency_seconds"),
            "search_calls": result.metrics.get("search_calls_total"),
            "fetch_urls": result.metrics.get("fetch_urls_total"),
        },
        # Extraction recall applies only to surfaced evidence. It is distinct
        # from reference-pool discovery recall below.
        "missed_on_page": None,
        "outcome_labels": {
            "mandatory_requirements_satisfied": None,
            "offering_scope_correct": None,
            "shortlist_qualifying_supplier_ids": None,
            "task_completion": None,
            "reference_pool_supplier_ids": None,
            # Must include model, provider, and failed-attempt spend. The
            # controller's token meter alone is intentionally insufficient.
            "total_attempted_cost_usd": None,
        },
    }


def _failed_case(case: dict[str, Any], exc: Exception) -> dict[str, Any]:
    return {
        "case_id": case["case_id"],
        "query": case["query"],
        "expected_mode": case["expected_mode"],
        "actual_mode": None,
        "mode_correct": False,
        "stop_reason": "run_error",
        "run_error": f"{type(exc).__name__}: {exc}",
        "fetch_outcomes": {},
        "run_id": None,
        "requirements": [],
        "requirements_confirmed": None,
        "supplier_sources": {},
        "candidates": [],
        "withheld_candidates": [],
        "rejected_observations": {},
        "operational_metrics": {"cost": None, "latency_seconds": None,
                                "search_calls": None, "fetch_urls": None},
        "missed_on_page": None,
        "outcome_labels": {
            "mandatory_requirements_satisfied": None,
            "offering_scope_correct": None,
            "shortlist_qualifying_supplier_ids": None,
            "task_completion": None,
            "reference_pool_supplier_ids": None,
            "total_attempted_cost_usd": None,
        },
    }


def _candidate_bundle(candidate: dict[str, Any], ledger: EvidenceLedger) -> dict[str, Any]:
    return {
        "supplier_id": candidate.get("supplier_id"),
        "offering_id": candidate.get("offering_id"),
        "fields": {key: candidate[key] for key in _FIELD_KEYS if candidate.get(key) is not None},
        # Every cited reference is shown. Do not make the first two citations
        # stand in for the evidence the controller actually used.
        "evidence": [_excerpt(ledger, ref) for ref in candidate.get("evidence_refs", [])],
        "extraction_correct": None,
    }


def _excerpt(ledger: EvidenceLedger, ref: dict[str, Any]) -> dict[str, Any]:
    item = ledger.get(ref.get("ledger_id", ""))
    if item is None:
        return {
            "ledger_id": ref.get("ledger_id"), "url": ref.get("url"), "text_excerpt": "",
            "text_preview": "", "preview_truncated": False, "full_text": None,
            "full_text_sha256": None, "retrieved_at": None, "source_tool": None,
        }
    full_text = item.text or item.snippet or ""
    preview = full_text[:EXCERPT_CHARS]
    return {
        "ledger_id": item.ledger_id,
        "url": item.final_url or item.url,
        "title": item.title,
        # text_excerpt remains for older graders. It is only a labelled preview;
        # full_text is the complete cited material available to the grader.
        "text_excerpt": preview,
        "text_preview": preview,
        "preview_truncated": len(full_text) > EXCERPT_CHARS,
        "full_text": full_text,
        "full_text_sha256": hashlib.sha256(full_text.encode("utf-8")).hexdigest(),
        "retrieved_at": item.retrieved_at,
        "source_tool": item.source_tool,
    }


def _ungraded_extraction(payload: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for case in payload.get("cases", []):
        if not isinstance(case.get("missed_on_page"), int):
            missing.append(f"{case.get('case_id')}: missed_on_page")
        for index, candidate in enumerate(case.get("candidates", [])):
            if not isinstance(candidate.get("extraction_correct"), bool):
                missing.append(f"{case.get('case_id')}: candidates[{index}].extraction_correct")
    return missing


def _operational_report(cases: list[dict[str, Any]], *, correctly_qualified: int | None) -> dict[str, Any]:
    """Aggregate all attempted runs, including empty and failed outcomes."""
    model_costs: list[float] = []
    attempted_costs: list[float] = []
    latencies: list[float] = []
    missing_model_cost = missing_attempted_cost = missing_latency = False
    search_calls = fetch_urls = 0
    missing_attempt_counts = False
    for case in cases:
        metrics = case.get("operational_metrics") or {}
        model_cost = (metrics.get("cost") or {}).get("total_usd") if isinstance(metrics.get("cost"), dict) else None
        explicit_cost = (case.get("outcome_labels") or {}).get("total_attempted_cost_usd")
        latency = (metrics.get("latency_seconds") or {}).get("total") if isinstance(metrics.get("latency_seconds"), dict) else metrics.get("elapsed_seconds")
        if isinstance(model_cost, (int, float)):
            model_costs.append(float(model_cost))
        else:
            missing_model_cost = True
        if isinstance(explicit_cost, (int, float)) and explicit_cost >= 0:
            attempted_costs.append(float(explicit_cost))
        else:
            missing_attempted_cost = True
        if isinstance(latency, (int, float)):
            latencies.append(float(latency))
        else:
            missing_latency = True
        if all(isinstance(metrics.get(key), int) for key in ("search_calls", "fetch_urls")):
            search_calls += metrics["search_calls"]
            fetch_urls += metrics["fetch_urls"]
        else:
            missing_attempt_counts = True
    emitted = sum(bool(case.get("candidates")) for case in cases)
    empty = len(cases) - emitted
    reported_model_cost = round(sum(model_costs), 6) if not missing_model_cost else None
    total_attempted_cost = round(sum(attempted_costs), 6) if not missing_attempted_cost else None
    total_latency = round(sum(latencies), 3) if not missing_latency else None
    return {
        "attempted_runs": len(cases),
        "emitted_runs": emitted,
        "empty_or_failed_runs": empty,
        "reported_model_cost_usd": reported_model_cost,
        "reported_model_cost_scope": "model-token meter only; excludes provider and failed-attempt spend",
        "total_attempted_cost_usd": total_attempted_cost,
        "total_attempted_cost_status": (
            "available: explicit human/operator supplied all-attempt cost" if total_attempted_cost is not None
            else "unavailable: supply outcome_labels.total_attempted_cost_usd for every attempted run"
        ),
        "cost_per_correctly_qualified_supplier_usd": (
            round(total_attempted_cost / correctly_qualified, 6)
            if total_attempted_cost is not None and correctly_qualified else None
        ),
        "mean_latency_seconds": (
            round(total_latency / len(cases), 3) if total_latency is not None and cases else None
        ),
        "attempted_search_calls": None if missing_attempt_counts else search_calls,
        "attempted_fetch_urls": None if missing_attempt_counts else fetch_urls,
    }


def score_human_outcomes(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate independently supplied human outcome labels across attempts.

    This function never reads verifier grades as truth. Every quality metric is
    null until every required outcome label is explicitly supplied.
    """
    required = (
        "mandatory_requirements_satisfied", "offering_scope_correct",
        "shortlist_qualifying_supplier_ids", "task_completion", "reference_pool_supplier_ids",
    )
    missing = [
        f"{case.get('case_id')}: outcome_labels.{field}"
        for case in cases
        for field in required
        if not isinstance(case.get("outcome_labels"), dict)
        or case["outcome_labels"].get(field) is None
    ]
    if missing:
        return {
            "status": "unavailable",
            "reason": "Human outcome labels are incomplete.",
            "missing_labels": missing,
            "mandatory_requirement_satisfaction_rate": None,
            "offering_scope_correctness_rate": None,
            "shortlist_precision": None,
            "task_completion_rate": None,
            "reference_pool_recall": None,
            "operational": _operational_report(cases, correctly_qualified=None),
        }

    mandatory = offering_scope = completed = 0
    returned = qualifying = reference = recalled = 0
    for case in cases:
        labels = case["outcome_labels"]
        if not isinstance(labels["mandatory_requirements_satisfied"], bool):
            raise ValueError(f"{case.get('case_id')}: mandatory_requirements_satisfied must be boolean.")
        if not isinstance(labels["offering_scope_correct"], bool):
            raise ValueError(f"{case.get('case_id')}: offering_scope_correct must be boolean.")
        if not isinstance(labels["task_completion"], bool):
            raise ValueError(f"{case.get('case_id')}: task_completion must be boolean.")
        qualifying_ids = _label_supplier_ids(case, labels, "shortlist_qualifying_supplier_ids")
        reference_ids = _label_supplier_ids(case, labels, "reference_pool_supplier_ids")
        returned_ids = {candidate.get("supplier_id") for candidate in case.get("candidates", []) if candidate.get("supplier_id")}
        if not qualifying_ids <= returned_ids:
            raise ValueError(f"{case.get('case_id')}: shortlist_qualifying_supplier_ids must be returned suppliers.")
        mandatory += labels["mandatory_requirements_satisfied"]
        offering_scope += labels["offering_scope_correct"]
        completed += labels["task_completion"]
        returned += len(returned_ids)
        qualifying += len(qualifying_ids)
        reference += len(reference_ids)
        recalled += len(returned_ids & reference_ids)
    return {
        "status": "available",
        "mandatory_requirement_satisfaction_rate": round(mandatory / len(cases), 3) if cases else None,
        "offering_scope_correctness_rate": round(offering_scope / len(cases), 3) if cases else None,
        "shortlist_precision": round(qualifying / returned, 3) if returned else None,
        "task_completion_rate": round(completed / len(cases), 3) if cases else None,
        "reference_pool_recall": round(recalled / reference, 3) if reference else None,
        "operational": _operational_report(cases, correctly_qualified=qualifying),
    }


def _label_supplier_ids(case: dict[str, Any], labels: dict[str, Any], field: str) -> set[str]:
    value = labels[field]
    if not isinstance(value, list):
        raise ValueError(f"{case.get('case_id')}: {field} must be a list of non-empty supplier IDs.")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ValueError(f"{case.get('case_id')}: {field} must contain only non-empty supplier ID strings.")
    normalized = [item.strip() for item in value]
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{case.get('case_id')}: {field} must not contain duplicate supplier IDs.")
    return set(normalized)


def score_live_sample(payload: dict[str, Any]) -> dict[str, Any]:
    """Score explicit human labels; absent labels remain unavailable."""
    cases = payload.get("cases", [])
    ungraded = _ungraded_extraction(payload)
    if ungraded:
        raise ValueError(
            f"{len(ungraded)} extraction grading field(s) are still ungraded: "
            + "; ".join(ungraded[:8]) + (" ..." if len(ungraded) > 8 else "")
            + ". Grade extraction_correct and missed_on_page before re-running check."
        )
    graded = [candidate for case in cases for candidate in case.get("candidates", [])]
    correct = sum(candidate["extraction_correct"] for candidate in graded)
    missed = sum(case["missed_on_page"] for case in cases)
    mode_correct = sum(bool(case.get("mode_correct")) for case in cases)
    per_mode: dict[str, dict[str, int]] = {}
    for case in cases:
        bucket = per_mode.setdefault(case["expected_mode"], {"cases": 0, "candidates": 0,
                                                               "correct": 0, "missed": 0})
        bucket["cases"] += 1
        bucket["candidates"] += len(case.get("candidates", []))
        bucket["correct"] += sum(candidate["extraction_correct"] for candidate in case.get("candidates", []))
        bucket["missed"] += case["missed_on_page"]
    return {
        "offline": payload.get("offline", False),
        "cases": len(cases),
        "candidates_graded": len(graded),
        "end_to_end_routing_accuracy": round(mode_correct / len(cases), 3) if cases else None,
        "extraction_precision": round(correct / len(graded), 3) if graded else None,
        "on_page_extraction_recall": round(correct / (correct + missed), 3) if correct + missed else None,
        "missed_on_page_total": missed,
        "per_mode": per_mode,
        "human_outcomes": score_human_outcomes(cases),
    }
