"""Synthetic fixtures exercise label-gated infrastructure; they are not human evaluation."""

from __future__ import annotations

import asyncio
import copy
import json
import math

from spider_qwen.benchmarks.stage3 import (
    group_disjoint_split,
    prepare_benchmark,
    release_gate,
    run_isolated_condition_matrix,
    validate_labelled_input,
    validate_independent_labels,
    weighted_outcome_report,
)


def _task(task_id: str, mode: str, site: str, supplier: str) -> dict:
    return {
        "task_id": task_id,
        "case_id": task_id,
        "mode": mode,
        "query": f"query for {task_id}",
        "site_families": [site],
        "supplier_families": [supplier],
        "candidates": [{"supplier_id": f"returned-{task_id}"}],
        "outcome_labels": {
            "mandatory_requirements_satisfied": True,
            "offering_scope_correct": True,
            "shortlist_qualifying_supplier_ids": [f"returned-{task_id}"],
            "task_completion": True,
            "reference_pool_supplier_ids": [f"returned-{task_id}"],
            "total_attempted_cost_usd": 1.25,
        },
    }


def _labelled_payload() -> dict:
    # Every component contains both modes, so the split can evaluate the stated
    # service/product mix on calibration and untouched groups.
    return {
        "label_provenance": "independent_human",
        "evaluation_condition": {"name": "cold", "state_isolation": "per_case"},
        "tasks": [
            _task("s1", "service_quote_required", "site-a", "supplier-a"),
            _task("p1", "product_exact_price", "site-a", "supplier-b"),
            _task("s2", "service_quote_required", "site-b", "supplier-c"),
            _task("p2", "product_exact_price", "site-b", "supplier-d"),
        ],
    }


def _weights() -> dict[str, float]:
    return {"service_quote_required": 0.6, "product_exact_price": 0.4}


def _prelabelled_payload(payload: dict) -> dict:
    prepared = copy.deepcopy(payload)
    for task in prepared["tasks"]:
        task.pop("outcome_labels", None)
    return prepared


def _thresholds() -> dict[str, float]:
    return {
        "mandatory_requirement_satisfaction_rate": 1.0,
        "offering_scope_correctness_rate": 1.0,
        "shortlist_precision": 1.0,
        "task_completion_rate": 1.0,
        "reference_pool_recall": 1.0,
    }


def test_group_disjoint_split_keeps_site_and_supplier_families_together() -> None:
    split = group_disjoint_split(_labelled_payload()["tasks"], seed="test")

    calibration_ids = {task["task_id"] for task in split["calibration"]}
    untouched_ids = {task["task_id"] for task in split["untouched_test"]}

    assert calibration_ids and untouched_ids
    assert not (calibration_ids & untouched_ids)
    assert split["family_overlap"] == {"site_families": [], "supplier_families": []}
    assert ({"s1", "p1"} <= calibration_ids) != ({"s1", "p1"} <= untouched_ids)


def test_group_disjoint_split_honours_fraction_for_disconnected_components() -> None:
    tasks = [
        _task(f"t{index}", "service_quote_required", f"site-{index}", f"supplier-{index}")
        for index in range(10)
    ]

    small_calibration = group_disjoint_split(tasks, calibration_fraction=0.2, seed="fraction")
    large_calibration = group_disjoint_split(tasks, calibration_fraction=0.8, seed="fraction")

    assert len(small_calibration["calibration"]) == 2
    assert len(small_calibration["untouched_test"]) == 8
    assert len(large_calibration["calibration"]) == 8
    assert len(large_calibration["untouched_test"]) == 2


def test_release_gate_requires_independent_complete_finite_labels() -> None:
    payload = _labelled_payload()
    payload["label_provenance"] = "synthetic_fixture"
    payload["tasks"][0]["outcome_labels"]["total_attempted_cost_usd"] = math.nan

    validation = validate_independent_labels(payload)
    gate = release_gate(payload, mode_weights=_weights(), thresholds=_thresholds())

    assert not validation["valid"]
    assert any("label_provenance" in reason for reason in validation["reasons"])
    assert any("finite non-negative" in reason for reason in validation["reasons"])
    assert gate["status"] == "blocked"


def test_ai_provisional_labels_can_be_reported_but_cannot_open_release_gate() -> None:
    payload = _labelled_payload()
    payload["label_provenance"] = "ai_provisional"

    validation = validate_labelled_input(payload)
    prepared = prepare_benchmark(_prelabelled_payload(payload))
    gate = release_gate(
        payload, mode_weights=_weights(), thresholds=_thresholds(), prepared_manifest=prepared["manifest"],
    )

    assert validation["valid"]
    assert validation["label_provenance"] == "ai_provisional"
    assert gate["status"] == "blocked"
    assert "requires label_provenance" in gate["reasons"][-1]


def test_weighted_mixed_mode_report_and_gate_use_supplied_labels_only() -> None:
    payload = _labelled_payload()
    prepared = prepare_benchmark(_prelabelled_payload(payload), seed="test")

    report = weighted_outcome_report(payload["tasks"], _weights())
    gate = release_gate(
        payload, mode_weights=_weights(), thresholds=_thresholds(), prepared_manifest=prepared["manifest"],
    )

    assert report["status"] == "available"
    assert report["weights"] == _weights()
    assert report["weighted"]["shortlist_precision"] == 1.0
    assert gate["status"] == "eligible_for_release_review"
    assert "no live quality claim" in gate["note"]


def test_weighted_report_keeps_unavailable_component_metric_unavailable() -> None:
    payload = _labelled_payload()
    for task in payload["tasks"]:
        if task["mode"] == "product_exact_price":
            task["candidates"] = []
            task["outcome_labels"]["shortlist_qualifying_supplier_ids"] = []

    report = weighted_outcome_report(payload["tasks"], _weights())

    assert report["status"] == "available"
    assert report["per_mode"]["product_exact_price"]["shortlist_precision"] is None
    assert report["weighted"]["shortlist_precision"] is None


def test_release_gate_requires_matching_prepared_manifest() -> None:
    payload = _labelled_payload()
    prepared = prepare_benchmark(_prelabelled_payload(payload))

    missing = release_gate(payload, mode_weights=_weights(), thresholds=_thresholds())
    mismatched = release_gate(
        payload,
        mode_weights=_weights(),
        thresholds=_thresholds(),
        prepared_manifest={**prepared["manifest"], "task_definition_sha256": "0" * 64},
    )

    assert missing["status"] == "blocked"
    assert "prepared benchmark manifest" in missing["reasons"][0]
    assert mismatched["status"] == "blocked"
    assert "membership_sha256" in mismatched["reasons"][0]


def test_prepared_manifest_binds_query_and_other_task_inputs() -> None:
    payload = _labelled_payload()
    prepared = prepare_benchmark(_prelabelled_payload(payload))
    payload["tasks"][0]["query"] = "a different buyer request"

    gate = release_gate(
        payload, mode_weights=_weights(), thresholds=_thresholds(), prepared_manifest=prepared["manifest"],
    )

    assert gate["status"] == "blocked"
    assert "task_definition_sha256" in gate["reasons"][0]


def test_isolated_condition_matrix_copies_only_seeded_components_into_fresh_roots(tmp_path) -> None:
    observed: list[tuple[str, str, str]] = []
    seed = tmp_path / "seed"
    seed.joinpath("memory").mkdir(parents=True)
    seed.joinpath("page_cache").mkdir()
    seed.joinpath("memory", "facts.json").write_text("memory", encoding="utf-8")
    seed.joinpath("page_cache", "urls.json").write_text("cache", encoding="utf-8")

    async def runner(case: dict, state_path, condition: str) -> dict:
        state_path.joinpath(case["task_id"]).write_text(condition, encoding="utf-8")
        observed.append((condition, case["task_id"], str(state_path)))
        cache_file = state_path.joinpath("page_cache", "urls.json")
        cache_content = cache_file.read_text(encoding="utf-8") if cache_file.is_file() else None
        if condition == "warm_cache" and case["task_id"] == "s1":
            seed.joinpath("page_cache", "urls.json").write_text("mutated source", encoding="utf-8")
        return {
            "case_id": case["task_id"], "condition": condition,
            "has_memory": state_path.joinpath("memory").is_dir(),
            "has_cache": state_path.joinpath("page_cache").is_dir(),
            "cache_content": cache_content,
        }

    matrix = asyncio.run(run_isolated_condition_matrix(
        _labelled_payload()["tasks"][:2], runner, state_seed_dir=seed,
    ))

    by_condition = {
        condition: [state for observed_condition, _, state in observed if observed_condition == condition]
        for condition in ("cold", "warm_memory", "warm_cache")
    }
    assert len(set(by_condition["cold"])) == 2
    assert len(set(by_condition["warm_memory"])) == 2
    assert len(set(by_condition["warm_cache"])) == 2
    assert by_condition["warm_memory"][0] != by_condition["warm_cache"][0]
    assert matrix["conditions"]["cold"]["state_isolation"] == "per_case"
    assert matrix["conditions"]["warm_memory"]["state_isolation"] == "per_case_seeded"
    assert all(row["has_memory"] and not row["has_cache"] for row in matrix["conditions"]["warm_memory"]["rows"])
    assert all(row["has_cache"] and not row["has_memory"] for row in matrix["conditions"]["warm_cache"]["rows"])
    assert all(row["cache_content"] == "cache" for row in matrix["conditions"]["warm_cache"]["rows"])


def test_stage3_cli_reports_ai_provisional_labels_but_gates_them_closed(tmp_path) -> None:
    from spider_qwen.benchmarks.stage3 import main

    payload = _labelled_payload()
    payload["label_provenance"] = "ai_provisional"
    path = tmp_path / "provisional.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    preparation_input = tmp_path / "prelabelled.json"
    preparation_input.write_text(json.dumps(_prelabelled_payload(payload)), encoding="utf-8")
    assert main(["prepare", str(preparation_input)]) == 0
    assert main(["report", str(path), "--weights", json.dumps(_weights())]) == 0
    prepared = tmp_path / "prepared.json"
    prepared.write_text(
        json.dumps({"preparation": prepare_benchmark(_prelabelled_payload(payload))}), encoding="utf-8",
    )
    assert main([
        "gate", str(path), "--weights", json.dumps(_weights()),
        "--thresholds", json.dumps(_thresholds()), "--manifest", str(prepared),
    ]) == 1
