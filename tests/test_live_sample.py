"""Live extraction sampling harness: template -> hand-grade -> measured
precision/recall. The template pairs every extracted field with the page text
it came from; an ungraded file must never produce a number.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spider_qwen.benchmarks.live_sample import build_live_sample, score_human_outcomes, score_live_sample

CASE_SET = Path("spider_qwen/benchmarks/live_validation_set.json")


def test_template_is_gradable_end_to_end(tmp_path):
    payload = build_live_sample(CASE_SET, sample_size=2, state_dir=tmp_path, offline=True)
    assert payload["offline"] is True
    assert payload["sample_size"] == 2
    assert "measures the mocks" in payload["method"]
    assert payload["sampling"]["method"] == "sha256_case_id_order"
    assert payload["evaluation_manifest"]["operator_profile"] == "offline_demo"
    assert len(payload["evaluation_manifest"]["source_digest_sha256"]) == 64
    assert len(payload["evaluation_manifest"]["evaluation_fingerprint"]) == 64
    for case in payload["cases"]:
        assert case["missed_on_page"] is None  # grading slot
        assert case["run_id"].startswith("run_")
        assert isinstance(case["mode_correct"], bool)
        assert case["fetch_outcomes"]  # taxonomy travels with the sample
        assert case["outcome_labels"]["task_completion"] is None
        assert case["candidates"], "offline service cases must validate candidates"
        for cand in case["candidates"]:
            assert cand["extraction_correct"] is None  # grading slot
            assert cand["fields"].get("vendor_name")
            assert cand["evidence"], "every graded row needs page text to grade against"
            assert cand["evidence"][0]["text_excerpt"]
            assert cand["evidence"][0]["full_text"]
            assert len(cand["evidence"][0]["full_text_sha256"]) == 64
            assert isinstance(cand["evidence"][0]["preview_truncated"], bool)
            assert cand["evidence"][0]["retrieved_at"]
            assert cand["evidence"][0]["source_tool"]


def test_template_sampling_is_deterministic_and_not_file_order(tmp_path):
    one = build_live_sample(CASE_SET, sample_size=1, state_dir=tmp_path / "a", offline=True)
    two = build_live_sample(CASE_SET, sample_size=2, state_dir=tmp_path / "b", offline=True)
    assert one["cases"][0]["case_id"] == two["cases"][0]["case_id"]


def test_template_retains_failed_attempt_for_human_outcome_grading(tmp_path):
    class FailingController:
        async def run(self, *_args, **_kwargs):
            raise RuntimeError("synthetic stalled adapter failure")

    payload = build_live_sample(
        CASE_SET, sample_size=1, state_dir=tmp_path, offline=True,
        controller=FailingController(),
    )
    (case,) = payload["cases"]
    assert case["stop_reason"] == "run_error"
    assert case["candidates"] == []
    assert "RuntimeError" in case["run_error"]
    assert case["outcome_labels"]["task_completion"] is None


def test_template_forwards_typed_case_inputs_before_a_failed_attempt(tmp_path):
    case_path = tmp_path / "typed-cases.json"
    requirement = {"requirement_id": "req-night", "text": "overnight work", "kind": "mandatory", "scope": "supplier"}
    case_path.write_text(json.dumps({"cases": [{
        "case_id": "typed", "query": "cleaning", "expected_mode": "service_quote_required",
        "requirements": [requirement], "requirements_confirmed": True,
        "supplier_sources": {"Example Cleaning": ["operator_shortlist"]},
    }]}), encoding="utf-8")

    class CaptureFailingController:
        kwargs = None

        async def run(self, _query, **kwargs):
            self.kwargs = kwargs
            raise RuntimeError("synthetic")

    controller = CaptureFailingController()
    build_live_sample(case_path, sample_size=1, state_dir=tmp_path / "state", offline=True,
                      controller=controller)
    assert controller.kwargs == {
        "mode": "auto", "requirements": [requirement], "requirements_confirmed": True,
        "supplier_sources": {"Example Cleaning": ["operator_shortlist"]},
    }


def _graded_payload() -> dict:
    return {
        "offline": False,
        "cases": [
            {
                "case_id": "c1", "expected_mode": "service_quote_required",
                "mode_correct": True, "missed_on_page": 2,
                "candidates": [
                    {"extraction_correct": True},
                    {"extraction_correct": False},
                ],
            },
            {
                "case_id": "c2", "expected_mode": "product_exact_price",
                "mode_correct": False, "missed_on_page": 0,
                "candidates": [{"extraction_correct": True}],
            },
        ],
    }


def test_score_computes_precision_and_recall():
    report = score_live_sample(_graded_payload())
    assert report["cases"] == 2
    assert report["candidates_graded"] == 3
    assert report["extraction_precision"] == round(2 / 3, 3)
    assert report["on_page_extraction_recall"] == 0.5  # 2 correct / (2 correct + 2 missed)
    assert report["end_to_end_routing_accuracy"] == 0.5
    assert report["per_mode"]["service_quote_required"]["missed"] == 2
    assert report["human_outcomes"]["status"] == "unavailable"
    assert report["human_outcomes"]["shortlist_precision"] is None


def test_score_reports_outcomes_only_from_explicit_human_labels():
    payload = _graded_payload()
    payload["cases"][0]["candidates"] = [
        {"supplier_id": "supplier-a", "extraction_correct": True},
        {"supplier_id": "supplier-b", "extraction_correct": False},
    ]
    payload["cases"][1]["candidates"] = [{"supplier_id": "supplier-c", "extraction_correct": True}]
    payload["cases"][0]["outcome_labels"] = {
        "mandatory_requirements_satisfied": True,
        "offering_scope_correct": False,
        "shortlist_qualifying_supplier_ids": ["supplier-a"],
        "task_completion": True,
        "reference_pool_supplier_ids": ["supplier-a", "supplier-d"],
    }
    payload["cases"][1]["outcome_labels"] = {
        "mandatory_requirements_satisfied": False,
        "offering_scope_correct": True,
        "shortlist_qualifying_supplier_ids": ["supplier-c"],
        "task_completion": False,
        "reference_pool_supplier_ids": ["supplier-c"],
    }
    outcomes = score_human_outcomes(payload["cases"])
    assert outcomes["status"] == "available"
    assert outcomes["shortlist_precision"] == round(2 / 3, 3)
    assert outcomes["reference_pool_recall"] == round(2 / 3, 3)
    assert outcomes["operational"]["total_attempted_cost_usd"] is None
    assert outcomes["operational"]["cost_per_correctly_qualified_supplier_usd"] is None


def test_synthetic_human_outcomes_include_empty_runs_cost_and_latency():
    # Synthetic labels exercise aggregation only; they are not a claim that a
    # human has evaluated the committed live-validation corpus.
    payload = {
        "offline": False,
        "cases": [
            {
                "case_id": "synthetic-returned", "expected_mode": "service_quote_required",
                "mode_correct": True, "missed_on_page": 0,
                "candidates": [
                    {"supplier_id": "supplier-a", "extraction_correct": True},
                    {"supplier_id": "supplier-b", "extraction_correct": True},
                ],
                "operational_metrics": {
                    "cost": {"total_usd": 3.0}, "latency_seconds": {"total": 2.0},
                    "search_calls": 2, "fetch_urls": 4,
                },
                "outcome_labels": {
                    "mandatory_requirements_satisfied": True,
                    "offering_scope_correct": True,
                    "shortlist_qualifying_supplier_ids": ["supplier-a"],
                    "task_completion": True,
                    "reference_pool_supplier_ids": ["supplier-a", "supplier-c"],
                    # Explicit all-attempt spend exceeds the model-token meter.
                    "total_attempted_cost_usd": 5.0,
                },
            },
            {
                "case_id": "synthetic-empty", "expected_mode": "service_quote_required",
                "mode_correct": True, "missed_on_page": 1, "candidates": [],
                "operational_metrics": {
                    "cost": {"total_usd": 1.0}, "latency_seconds": {"total": 6.0},
                    "search_calls": 1, "fetch_urls": 2,
                },
                "outcome_labels": {
                    "mandatory_requirements_satisfied": False,
                    "offering_scope_correct": False,
                    "shortlist_qualifying_supplier_ids": [],
                    "task_completion": False,
                    "reference_pool_supplier_ids": ["supplier-d"],
                    "total_attempted_cost_usd": 2.0,
                },
            },
        ],
    }
    outcomes = score_live_sample(payload)["human_outcomes"]
    assert outcomes["shortlist_precision"] == 0.5
    assert outcomes["task_completion_rate"] == 0.5
    assert outcomes["reference_pool_recall"] == round(1 / 3, 3)
    assert outcomes["operational"] == {
        "attempted_runs": 2,
        "emitted_runs": 1,
        "empty_or_failed_runs": 1,
        "reported_model_cost_usd": 4.0,
        "reported_model_cost_scope": "model-token meter only; excludes provider and failed-attempt spend",
        "total_attempted_cost_usd": 7.0,
        "total_attempted_cost_status": "available: explicit human/operator supplied all-attempt cost",
        "cost_per_correctly_qualified_supplier_usd": 7.0,
        "mean_latency_seconds": 4.0,
        "attempted_search_calls": 3,
        "attempted_fetch_urls": 6,
    }


def test_outcome_supplier_labels_reject_invalid_ids():
    payload = _graded_payload()
    for case in payload["cases"]:
        case["outcome_labels"] = {
            "mandatory_requirements_satisfied": True,
            "offering_scope_correct": True,
            "shortlist_qualifying_supplier_ids": [],
            "task_completion": True,
            "reference_pool_supplier_ids": [],
        }
    payload["cases"][0]["candidates"] = [{"supplier_id": "supplier-a", "extraction_correct": True}]
    payload["cases"][0]["outcome_labels"]["shortlist_qualifying_supplier_ids"] = ["supplier-a", "supplier-a"]
    with pytest.raises(ValueError, match="duplicate supplier IDs"):
        score_human_outcomes(payload["cases"])


def test_score_refuses_ungraded_file_with_case_ids():
    payload = _graded_payload()
    payload["cases"][0]["candidates"][1]["extraction_correct"] = None
    payload["cases"][1]["missed_on_page"] = None
    with pytest.raises(ValueError, match=r"c1.*c2|2 grading"):
        score_live_sample(payload)


def test_cli_template_and_check_roundtrip(tmp_path, monkeypatch):
    from spider_qwen.api.cli import main

    monkeypatch.setenv("SPIDER_QWEN_STATE_DIR", str(tmp_path / "state"))
    out = tmp_path / "live_sample.json"
    assert main(["live-sample", "template", "--offline", "--sample", "1",
                 "--out", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))

    # check refuses the ungraded template loudly ...
    assert main(["live-sample", "check", str(out)]) == 1

    # ... and reports numbers once every slot is graded.
    for case in payload["cases"]:
        case["missed_on_page"] = 1
        for cand in case["candidates"]:
            cand["extraction_correct"] = True
    out.write_text(json.dumps(payload), encoding="utf-8")
    assert main(["live-sample", "check", str(out)]) == 0
