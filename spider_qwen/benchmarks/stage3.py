"""Label-gated infrastructure for mixed procurement outcome evaluation.

This module never creates labels. It validates supplied label provenance,
keeps site/supplier families disjoint between calibration and untouched test,
and refuses release decisions without complete labels and explicit thresholds.
"""

from __future__ import annotations

import asyncio
import argparse
import hashlib
import json
import math
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Awaitable, Callable

from .live_sample import score_human_outcomes

_QUALITY_METRICS = (
    "mandatory_requirement_satisfaction_rate",
    "offering_scope_correctness_rate",
    "shortlist_precision",
    "task_completion_rate",
    "reference_pool_recall",
)
_LABEL_FIELDS = (
    "mandatory_requirements_satisfied", "offering_scope_correct",
    "shortlist_qualifying_supplier_ids", "task_completion", "reference_pool_supplier_ids",
    "total_attempted_cost_usd",
)
_CONDITIONS = ("cold", "warm_memory", "warm_cache")
_ALLOWED_LABEL_PROVENANCE = ("independent_human", "ai_provisional")
_TASK_OUTPUT_FIELDS = frozenset({
    "candidate_annotations", "candidates", "evidence_bundle", "metrics", "operational_metrics",
    "outcome_labels", "run_id", "run_metadata", "verifier_grades",
})


def _finite_nonnegative_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and value >= 0
    )


def _finite_positive_number(value: Any) -> bool:
    return _finite_nonnegative_number(value) and value > 0


def validate_labelled_input(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate supplied labels without judging their truth or release suitability."""
    reasons: list[str] = []
    tasks = payload.get("tasks")
    provenance = payload.get("label_provenance")
    if provenance not in _ALLOWED_LABEL_PROVENANCE:
        reasons.append("label_provenance must explicitly be 'independent_human' or 'ai_provisional'.")
    if not isinstance(tasks, list) or not tasks:
        return {
            "valid": False, "reasons": reasons + ["tasks must be a non-empty list."],
            "tasks": [], "label_provenance": provenance,
        }
    seen_ids: set[str] = set()
    for index, task in enumerate(tasks):
        prefix = f"tasks[{index}]"
        if not isinstance(task, dict):
            reasons.append(f"{prefix} must be an object.")
            continue
        task_id = task.get("task_id") or task.get("case_id")
        if not isinstance(task_id, str) or not task_id.strip():
            reasons.append(f"{prefix}.task_id is required.")
        elif task_id in seen_ids:
            reasons.append(f"duplicate task_id {task_id!r}.")
        else:
            seen_ids.add(task_id)
        if not isinstance(task.get("mode"), str) or not task["mode"].strip():
            reasons.append(f"{prefix}.mode is required.")
        for field in ("site_families", "supplier_families"):
            families = task.get(field)
            if not isinstance(families, list) or any(
                not isinstance(value, str) or not value.strip() for value in families
            ):
                reasons.append(f"{prefix}.{field} must be a list of explicit non-empty family IDs.")
        labels = task.get("outcome_labels")
        if not isinstance(labels, dict):
            reasons.append(f"{prefix}.outcome_labels is required.")
            continue
        for field in _LABEL_FIELDS:
            if labels.get(field) is None:
                reasons.append(f"{prefix}.outcome_labels.{field} is missing.")
        if not _finite_nonnegative_number(labels.get("total_attempted_cost_usd")):
            reasons.append(
                f"{prefix}.outcome_labels.total_attempted_cost_usd must be a finite non-negative number."
            )
        # Reuse the scorer's strict supplier-ID validation; it remains entirely
        # label-driven and does not invoke the verifier.
        try:
            scored = score_human_outcomes([task])
            if scored["status"] != "available":
                reasons.append(f"{prefix} has incomplete human outcome labels.")
        except (TypeError, ValueError) as exc:
            reasons.append(f"{prefix} invalid outcome labels: {exc}")
    return {"valid": not reasons, "reasons": reasons, "tasks": tasks, "label_provenance": provenance}


def validate_independent_labels(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate labels that may support a release decision; AI labels cannot do so."""
    validation = validate_labelled_input(payload)
    if validation["label_provenance"] != "independent_human":
        return {
            **validation,
            "valid": False,
            "reasons": [*validation["reasons"], "Release evaluation requires label_provenance 'independent_human'."],
        }
    return validation


def _structural_task_errors(tasks: Any) -> list[str]:
    if not isinstance(tasks, list) or not tasks:
        return ["tasks must be a non-empty list."]
    errors: list[str] = []
    seen_ids: set[str] = set()
    for index, task in enumerate(tasks):
        prefix = f"tasks[{index}]"
        if not isinstance(task, dict):
            errors.append(f"{prefix} must be an object.")
            continue
        task_id = task.get("task_id") or task.get("case_id")
        if not isinstance(task_id, str) or not task_id.strip():
            errors.append(f"{prefix}.task_id is required.")
        elif task_id in seen_ids:
            errors.append(f"duplicate task_id {task_id!r}.")
        else:
            seen_ids.add(task_id)
        if not isinstance(task.get("mode"), str) or not task["mode"].strip():
            errors.append(f"{prefix}.mode is required.")
        for field in ("site_families", "supplier_families"):
            families = task.get(field)
            if not isinstance(families, list) or any(
                not isinstance(value, str) or not value.strip() for value in families
            ):
                errors.append(f"{prefix}.{field} must be a list of explicit non-empty family IDs.")
    return errors


def _task_id(task: dict[str, Any]) -> str:
    return str(task.get("task_id") or task.get("case_id"))


def _task_definition_hash(tasks: list[dict[str, Any]]) -> str:
    definitions = sorted(
        ({key: value for key, value in task.items()
          if key not in _TASK_OUTPUT_FIELDS and not key.endswith("_provenance")}
         for task in tasks),
        key=lambda value: value.get("task_id") or value.get("case_id"),
    )
    return hashlib.sha256(json.dumps(definitions, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _condition_errors(condition: Any) -> list[str]:
    if not isinstance(condition, dict):
        return ["evaluation_condition must describe the measured state condition."]
    name = condition.get("name")
    isolation = condition.get("state_isolation")
    if name == "cold" and isolation == "per_case":
        return []
    if name in ("warm_memory", "warm_cache") and isolation == "per_case_seeded":
        seed_hash = condition.get("seed_snapshot_sha256")
        if isinstance(seed_hash, str) and len(seed_hash) == 64 and all(char in "0123456789abcdef" for char in seed_hash.lower()):
            return []
        return ["warm evaluation_condition requires a seed_snapshot_sha256."]
    return ["evaluation_condition must be cold/per_case or warm_memory|warm_cache/per_case_seeded."]


def prepare_benchmark(
    payload: dict[str, Any], *, calibration_fraction: float = 0.5, seed: str = "stage3-v1",
) -> dict[str, Any]:
    """Commit group membership before outcome labels are collected."""
    provenance = payload.get("label_provenance")
    errors = _structural_task_errors(payload.get("tasks"))
    if provenance not in _ALLOWED_LABEL_PROVENANCE:
        errors.append("label_provenance must explicitly be 'independent_human' or 'ai_provisional'.")
    errors.extend(_condition_errors(payload.get("evaluation_condition")))
    if any(
        isinstance(task, dict)
        and isinstance(task.get("outcome_labels"), dict)
        and any(value is not None for value in task["outcome_labels"].values())
        for task in payload.get("tasks", [])
    ):
        errors.append("Prepare membership before task-level outcome labels are recorded.")
    if errors:
        return {"status": "unavailable", "reasons": errors}
    tasks = payload["tasks"]
    try:
        split = group_disjoint_split(tasks, calibration_fraction=calibration_fraction, seed=seed)
    except ValueError as exc:
        return {"status": "unavailable", "reasons": [str(exc)]}
    manifest = {
        "schema_version": 1,
        "label_provenance": provenance,
        "evaluation_condition": payload["evaluation_condition"],
        "calibration_task_ids": sorted(_task_id(task) for task in split["calibration"]),
        "untouched_test_task_ids": sorted(_task_id(task) for task in split["untouched_test"]),
        "task_definition_sha256": _task_definition_hash(tasks),
        "prepared_before_outcome_labels": True,
    }
    manifest["membership_sha256"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {"status": "prepared", "manifest": manifest, "split": split}


def group_disjoint_split(
    tasks: list[dict[str, Any]], *, calibration_fraction: float = 0.5, seed: str = "stage3-v1",
) -> dict[str, Any]:
    """Split connected site/supplier families without leakage between splits."""
    if not 0.0 < calibration_fraction < 1.0:
        raise ValueError("calibration_fraction must be strictly between 0 and 1.")
    parents = list(range(len(tasks)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left, right = find(left), find(right)
        if left != right:
            parents[right] = left

    owners: dict[tuple[str, str], int] = {}
    for index, task in enumerate(tasks):
        for family_type in ("site_families", "supplier_families"):
            for family in task.get(family_type, []):
                key = (family_type, family)
                if key in owners:
                    union(index, owners[key])
                else:
                    owners[key] = index
    components: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for index, task in enumerate(tasks):
        components[find(index)].append(task)
    groups = list(components.values())
    if len(groups) < 2:
        raise ValueError("Cannot make a group-disjoint calibration/test split: all tasks share one family component.")
    groups.sort(key=lambda group: hashlib.sha256(
        (seed + "|" + "|".join(sorted(str(task.get("task_id") or task.get("case_id")) for task in group))).encode("utf-8")
    ).hexdigest())
    target = len(tasks) * calibration_fraction
    calibration: list[dict[str, Any]] = []
    untouched: list[dict[str, Any]] = []
    for index, group in enumerate(groups):
        # Leave at least one complete component untouched. Greedy assignment is
        # deterministic, honours the requested task-count fraction where group
        # sizes permit, and keeps every connected family on one side.
        remaining_components = len(groups) - index
        if len(calibration) < target and remaining_components > 1:
            calibration.extend(group)
        else:
            untouched.extend(group)
    if not untouched:
        moved = groups[-1]
        calibration = [task for task in calibration if task not in moved]
        untouched = list(moved)
    calibration_families = _families(calibration)
    untouched_families = _families(untouched)
    overlap = {
        field: sorted(calibration_families[field] & untouched_families[field])
        for field in calibration_families
    }
    if any(overlap.values()):  # defensive: union-find should make this impossible
        raise AssertionError(f"family leakage in group-disjoint split: {overlap}")
    return {
        "calibration": calibration,
        "untouched_test": untouched,
        "family_overlap": overlap,
        "components": len(groups),
    }


def _families(tasks: list[dict[str, Any]]) -> dict[str, set[str]]:
    return {
        field: {family for task in tasks for family in task.get(field, [])}
        for field in ("site_families", "supplier_families")
    }


def weighted_outcome_report(tasks: list[dict[str, Any]], mode_weights: dict[str, float]) -> dict[str, Any]:
    """Weight service/product (or other) mode reports without blending labels."""
    modes = {str(task.get("mode")) for task in tasks}
    if not modes or set(mode_weights) != modes or any(
        not _finite_positive_number(weight) for weight in mode_weights.values()
    ):
        return {"status": "unavailable", "reason": "Provide one positive explicit weight for every represented mode."}
    by_mode: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for task in tasks:
        by_mode[task["mode"]].append(task)
    reports = {mode: score_human_outcomes(items) for mode, items in by_mode.items()}
    if any(report["status"] != "available" for report in reports.values()):
        return {"status": "unavailable", "reason": "Outcome labels are incomplete.", "per_mode": reports}
    total_weight = sum(float(weight) for weight in mode_weights.values())
    weighted = {
        metric: (
            round(sum(reports[mode][metric] * float(mode_weights[mode]) for mode in reports) / total_weight, 3)
            if all(reports[mode][metric] is not None for mode in reports) else None
        )
        for metric in _QUALITY_METRICS
    }
    return {"status": "available", "weights": mode_weights, "per_mode": reports, "weighted": weighted}


def release_gate(
    payload: dict[str, Any], *, mode_weights: dict[str, float], thresholds: dict[str, float],
    prepared_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Fail closed unless labels and a precommitted untouched membership exist."""
    validation = validate_independent_labels(payload)
    if not validation["valid"]:
        return {"status": "blocked", "reasons": validation["reasons"], "validation": validation}
    manifest_errors = _manifest_errors(prepared_manifest, validation["tasks"])
    if manifest_errors:
        return {"status": "blocked", "reasons": manifest_errors, "validation": validation}
    assert prepared_manifest is not None  # narrowed by _manifest_errors
    missing_thresholds = [metric for metric in _QUALITY_METRICS if metric not in thresholds]
    invalid_thresholds = [
        metric for metric in _QUALITY_METRICS
        if metric in thresholds
        and (not _finite_nonnegative_number(thresholds[metric]) or thresholds[metric] > 1)
    ]
    if missing_thresholds or invalid_thresholds:
        detail = []
        if missing_thresholds:
            detail.append("Missing release thresholds: " + ", ".join(missing_thresholds))
        if invalid_thresholds:
            detail.append("Release thresholds must be finite numbers from 0 through 1: " + ", ".join(invalid_thresholds))
        return {"status": "blocked", "reasons": [
            *detail
        ], "validation": validation}
    by_id = {_task_id(task): task for task in validation["tasks"]}
    split = {
        "calibration": [by_id[task_id] for task_id in prepared_manifest["calibration_task_ids"]],
        "untouched_test": [by_id[task_id] for task_id in prepared_manifest["untouched_test_task_ids"]],
        "family_overlap": {
            field: sorted(_families([by_id[task_id] for task_id in prepared_manifest["calibration_task_ids"]])[field]
                          & _families([by_id[task_id] for task_id in prepared_manifest["untouched_test_task_ids"]])[field])
            for field in ("site_families", "supplier_families")
        },
        "manifest": prepared_manifest,
    }
    calibration = weighted_outcome_report(split["calibration"], mode_weights)
    untouched = weighted_outcome_report(split["untouched_test"], mode_weights)
    if calibration["status"] != "available" or untouched["status"] != "available":
        return {"status": "blocked", "reasons": ["Weighted label report is unavailable."],
                "split": split, "calibration": calibration, "untouched_test": untouched}
    failures = [
        f"untouched_test.{metric}={untouched['weighted'][metric]} below {thresholds[metric]}"
        for metric in _QUALITY_METRICS
        if untouched["weighted"][metric] is None
        or untouched["weighted"][metric] < thresholds[metric]
    ]
    return {
        "status": "blocked" if failures else "eligible_for_release_review",
        "reasons": failures,
        "split": split,
        "calibration": calibration,
        "untouched_test": untouched,
        "note": "Eligibility reflects supplied independent-human labels; no live quality claim is generated here.",
    }


def _manifest_errors(manifest: Any, tasks: list[dict[str, Any]]) -> list[str]:
    if not isinstance(manifest, dict):
        return ["A prepared benchmark manifest is required before release evaluation."]
    required = {
        "schema_version", "label_provenance", "evaluation_condition", "calibration_task_ids",
        "untouched_test_task_ids", "task_definition_sha256", "membership_sha256",
        "prepared_before_outcome_labels",
    }
    missing = sorted(required - set(manifest))
    if missing:
        return ["Prepared manifest is missing: " + ", ".join(missing)]
    unsigned = {key: manifest[key] for key in required - {"membership_sha256"}}
    expected_hash = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if manifest["membership_sha256"] != expected_hash:
        return ["Prepared manifest membership_sha256 does not match its recorded membership."]
    if manifest["schema_version"] != 1:
        return ["Prepared manifest schema_version is unsupported."]
    if manifest["prepared_before_outcome_labels"] is not True:
        return ["Prepared manifest must attest that membership preceded outcome labels."]
    if manifest["label_provenance"] != "independent_human":
        return ["Prepared manifest label_provenance must be 'independent_human' for release."]
    condition_errors = _condition_errors(manifest["evaluation_condition"])
    if condition_errors:
        return condition_errors
    if manifest["task_definition_sha256"] != _task_definition_hash(tasks):
        return ["Prepared manifest task_definition_sha256 does not match the labelled tasks."]
    calibration_ids = manifest["calibration_task_ids"]
    untouched_ids = manifest["untouched_test_task_ids"]
    if not isinstance(calibration_ids, list) or not isinstance(untouched_ids, list):
        return ["Prepared manifest task memberships must be lists."]
    if not all(isinstance(value, str) and value for value in calibration_ids + untouched_ids):
        return ["Prepared manifest task memberships must be non-empty task ID strings."]
    if len(set(calibration_ids)) != len(calibration_ids) or len(set(untouched_ids)) != len(untouched_ids):
        return ["Prepared manifest task memberships must not contain duplicate IDs."]
    if set(calibration_ids) & set(untouched_ids):
        return ["Prepared manifest calibration and untouched memberships overlap."]
    if set(calibration_ids) | set(untouched_ids) != {_task_id(task) for task in tasks}:
        return ["Prepared manifest membership does not exactly match the labelled tasks."]
    calibration_families = _families([task for task in tasks if _task_id(task) in set(calibration_ids)])
    untouched_families = _families([task for task in tasks if _task_id(task) in set(untouched_ids)])
    if any(calibration_families[field] & untouched_families[field] for field in calibration_families):
        return ["Prepared manifest leaks a site or supplier family into both splits."]
    return []


async def run_isolated_condition_matrix(
    cases: list[dict[str, Any]],
    runner: Callable[[dict[str, Any], Path, str], Awaitable[dict[str, Any]]],
    *, conditions: tuple[str, ...] = _CONDITIONS, state_seed_dir: Path | None = None,
    memory_subdirs: tuple[str, ...] = ("memory",), cache_subdirs: tuple[str, ...] = ("page_cache",),
) -> dict[str, Any]:
    """Run empty cold state or selective immutable snapshots in fresh case roots.

    ``state_seed_dir`` is copied into a private immutable snapshot once. The function
    never performs an unmetered warm-up: warm-memory copies only ``memory``
    subdirectories and warm-cache copies only ``page_cache`` subdirectories before
    each case. The runner must use the supplied root for all mutable state.
    """
    if not conditions or any(condition not in _CONDITIONS for condition in conditions):
        raise ValueError(f"conditions must be drawn from {_CONDITIONS}.")
    needs_seed = any(condition != "cold" for condition in conditions)
    if needs_seed and (state_seed_dir is None or not state_seed_dir.is_dir()):
        raise ValueError("warm conditions require an existing state_seed_dir snapshot.")
    seed_hash: str | None = None
    reports: dict[str, dict[str, Any]] = {}
    with tempfile.TemporaryDirectory(prefix="spider-qwen-condition-matrix-") as root:
        matrix_root = Path(root)
        staged_seed: Path | None = None
        if needs_seed:
            assert state_seed_dir is not None
            _reject_symlinked_seed_paths(state_seed_dir)
            staged_seed = matrix_root / "immutable-seed"
            shutil.copytree(state_seed_dir, staged_seed)
            seed_hash = _directory_hash(staged_seed)
        for condition in conditions:
            root_path = matrix_root / condition
            root_path.mkdir()
            rows: list[dict[str, Any]] = []
            components = () if condition == "cold" else (
                memory_subdirs if condition == "warm_memory" else cache_subdirs
            )
            for index, case in enumerate(cases):
                case_state = root_path / f"case-{index}"
                case_state.mkdir()
                for component in components:
                    _copy_seed_component(staged_seed, component, case_state)
                rows.append(await runner(case, case_state, condition))
            reports[condition] = {
                "state_isolation": "per_case" if condition == "cold" else "per_case_seeded",
                "seed_snapshot_sha256": seed_hash,
                "seed_components": list(components),
                "rows": rows,
            }
    return {"conditions": reports}


def _copy_seed_component(seed_root: Path | None, component: str, destination: Path) -> None:
    if not isinstance(component, str) or not component or Path(component).is_absolute() or ".." in Path(component).parts:
        raise ValueError("seed component paths must be non-empty relative paths.")
    assert seed_root is not None
    source = seed_root / component
    if not source.is_dir():
        raise ValueError(f"state_seed_dir is missing required seed component {component!r}.")
    shutil.copytree(source, destination / component)


def _directory_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda candidate: candidate.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _reject_symlinked_seed_paths(root: Path) -> None:
    resolved_root = root.resolve()
    if root.is_symlink():
        raise ValueError("state_seed_dir must not be a symbolic link.")
    for path in root.rglob("*"):
        if path.is_symlink() or not path.resolve().is_relative_to(resolved_root):
            raise ValueError("state_seed_dir must not contain symbolic links or paths outside its root.")


def _read_payload(path: str) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("Benchmark input must be a JSON object.")
    return value


def _json_object(value: str, option: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{option} must be a JSON object: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{option} must be a JSON object.")
    return parsed


def _read_manifest(path: str) -> dict[str, Any]:
    value = _read_payload(path)
    preparation = value.get("preparation")
    manifest = value.get("manifest") or (preparation.get("manifest") if isinstance(preparation, dict) else value)
    if not isinstance(manifest, dict):
        raise ValueError("Prepared manifest must be a JSON object or an object containing 'manifest'.")
    return manifest


def main(argv: list[str] | None = None) -> int:
    """Prepare, report, or gate a pre-labelled benchmark JSON file.

    This CLI never runs live procurement or produces outcome labels.  Execution
    of isolated cold/warm conditions is available to an application through
    ``run_isolated_condition_matrix`` because it needs a concrete runner.
    """
    parser = argparse.ArgumentParser(prog="python -m spider_qwen.benchmarks.stage3")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare", help="commit a disjoint split before outcome labels are collected")
    prepare.add_argument("input")
    prepare.add_argument("--calibration-fraction", type=float, default=0.5)
    prepare.add_argument("--seed", default="stage3-v1")
    report = subparsers.add_parser("report", help="produce a mixed-mode outcome report from labels")
    report.add_argument("input")
    report.add_argument("--weights", required=True, help="JSON object mapping every represented mode to its weight")
    gate = subparsers.add_parser("gate", help="apply release thresholds to a group-disjoint untouched split")
    gate.add_argument("input")
    gate.add_argument("--weights", required=True, help="JSON object mapping every represented mode to its weight")
    gate.add_argument("--thresholds", required=True, help="JSON object mapping every quality metric to a 0..1 threshold")
    gate.add_argument("--manifest", required=True, help="prepared manifest JSON from the prepare command")
    args = parser.parse_args(argv)
    try:
        payload = _read_payload(args.input)
        validation = validate_labelled_input(payload)
        if args.command == "prepare":
            output = {"preparation": prepare_benchmark(
                payload, calibration_fraction=args.calibration_fraction, seed=args.seed,
            ), "label_validation": validation}
            status = output["preparation"]["status"] == "prepared"
        elif args.command == "report":
            weights = _json_object(args.weights, "--weights")
            output = {
                "validation": validation,
                "provisional": validation["label_provenance"] == "ai_provisional",
                "note": (
                    "AI-provisional labels are diagnostic only and cannot support release."
                    if validation["label_provenance"] == "ai_provisional" else None
                ),
            }
            output["report"] = (
                weighted_outcome_report(validation["tasks"], weights)
                if validation["valid"] else {"status": "unavailable", "reason": "Supplied labels are invalid."}
            )
            status = validation["valid"] and output["report"]["status"] == "available"
        else:
            output = release_gate(
                payload,
                mode_weights=_json_object(args.weights, "--weights"),
                thresholds=_json_object(args.thresholds, "--thresholds"),
                prepared_manifest=_read_manifest(args.manifest),
            )
            status = output["status"] == "eligible_for_release_review"
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(output, indent=2, sort_keys=True, default=str))
    return 0 if status else 1


if __name__ == "__main__":
    raise SystemExit(main())
