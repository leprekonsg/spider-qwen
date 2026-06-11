"""Live extraction sampling harness: template -> hand-grade -> measured
precision/recall. The template pairs every extracted field with the page text
it came from; an ungraded file must never produce a number.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spider_qwen.benchmarks.live_sample import build_live_sample, score_live_sample

CASE_SET = Path("spider_qwen/benchmarks/live_validation_set.json")


def test_template_is_gradable_end_to_end(tmp_path):
    payload = build_live_sample(CASE_SET, sample_size=2, state_dir=tmp_path, offline=True)
    assert payload["offline"] is True
    assert payload["sample_size"] == 2
    assert "measures the mocks" in payload["method"]
    for case in payload["cases"]:
        assert case["missed_on_page"] is None  # grading slot
        assert case["run_id"].startswith("run_")
        assert isinstance(case["mode_correct"], bool)
        assert case["fetch_outcomes"]  # taxonomy travels with the sample
        assert case["candidates"], "offline service cases must validate candidates"
        for cand in case["candidates"]:
            assert cand["extraction_correct"] is None  # grading slot
            assert cand["fields"].get("vendor_name")
            assert cand["evidence"], "every graded row needs page text to grade against"
            assert cand["evidence"][0]["text_excerpt"]


def test_template_sampling_is_deterministic_first_n(tmp_path):
    one = build_live_sample(CASE_SET, sample_size=1, state_dir=tmp_path / "a", offline=True)
    two = build_live_sample(CASE_SET, sample_size=2, state_dir=tmp_path / "b", offline=True)
    assert one["cases"][0]["case_id"] == two["cases"][0]["case_id"]


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
    assert report["extraction_recall"] == 0.5  # 2 correct / (2 correct + 2 missed)
    assert report["mode_accuracy"] == 0.5
    assert report["per_mode"]["service_quote_required"]["missed"] == 2


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
