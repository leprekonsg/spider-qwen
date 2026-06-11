"""Conformal calibration harness: `spider-qwen calibrate template|check`.

The template harvests verifier-scored claim rows from finished runs into a
gradable JSON file; check validates the grading and reports the fitted
threshold. An ungraded file must fail loudly everywhere it is consumed.
"""

from __future__ import annotations

import json

import pytest

from spider_qwen.api.cli import main


def _run_cli(capsys, argv: list[str]) -> dict:
    rc = main(argv)
    out = capsys.readouterr().out
    assert rc == 0, out
    return json.loads(out)


@pytest.fixture()
def judged_run(capsys, tmp_path, monkeypatch) -> tuple[str, object]:
    monkeypatch.setenv("SPIDER_QWEN_STATE_DIR", str(tmp_path))
    result = _run_cli(capsys, ["run", "office cleaning Singapore", "--offline", "--judged-demo"])
    return result["run_id"], tmp_path


def test_template_harvests_verifier_scored_claims(capsys, judged_run):
    run_id, tmp_path = judged_run
    out_file = tmp_path / "calibration.json"
    summary = _run_cli(capsys, ["calibrate", "template", run_id, "--out", str(out_file)])
    assert summary["claims"] >= 1
    assert "calibrate check" in summary["next"]

    payload = json.loads(out_file.read_text(encoding="utf-8"))
    assert payload["alpha"] == 0.1
    for example in payload["examples"]:
        assert isinstance(example["verifier_score"], float)
        assert example["prediction_correct"] is None  # graded by a human, never auto-filled
        assert example["claim"]["run_id"] == run_id
        assert example["claim"]["ledger_id"].startswith("ev_")


def test_check_refuses_ungraded_template(capsys, judged_run):
    run_id, tmp_path = judged_run
    out_file = tmp_path / "calibration.json"
    _run_cli(capsys, ["calibrate", "template", run_id, "--out", str(out_file)])

    rc = main(["calibrate", "check", str(out_file)])
    err = capsys.readouterr().err
    assert rc == 1
    assert "ungraded" in err


def test_run_startup_rejects_ungraded_calibration_file(capsys, judged_run, monkeypatch):
    run_id, tmp_path = judged_run
    out_file = tmp_path / "calibration.json"
    _run_cli(capsys, ["calibrate", "template", run_id, "--out", str(out_file)])

    monkeypatch.setenv("SPIDER_QWEN_CONFORMAL_CALIBRATION", str(out_file))
    rc = main(["run", "office cleaning Singapore", "--offline"])
    err = capsys.readouterr().err
    assert rc == 2  # fail before any budget is spent, not mid-run
    assert "SPIDER_QWEN_CONFORMAL_CALIBRATION" in err


def _graded_file(tmp_path, scores: list[float], alpha: float = 0.1):
    path = tmp_path / "graded.json"
    path.write_text(json.dumps({
        "alpha": alpha,
        "examples": [{"verifier_score": s, "prediction_correct": True} for s in scores],
    }), encoding="utf-8")
    return path


def test_check_reports_threshold_for_sufficient_grading(capsys, tmp_path):
    scores = [0.95, 0.9, 0.88, 0.85, 0.8, 0.92, 0.91, 0.87, 0.83, 0.93, 0.89, 0.86]
    path = _graded_file(tmp_path, scores)
    report = _run_cli(capsys, ["calibrate", "check", str(path)])
    assert report["calibrated"] is True
    assert 0.0 < report["threshold"] <= 1.0
    assert report["calibration_size"] == len(scores)
    assert "SPIDER_QWEN_CONFORMAL_CALIBRATION" in report["activate"]


def test_check_refuses_too_few_correct_examples(capsys, tmp_path):
    path = _graded_file(tmp_path, [0.9, 0.8, 0.7])  # alpha=0.1 needs at least 9
    rc = main(["calibrate", "check", str(path)])
    report = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert report["calibrated"] is False
    assert any("need at least 9" in r for r in report["reasons"])


def test_check_actionable_on_missing_file(capsys, tmp_path):
    rc = main(["calibrate", "check", str(tmp_path / "missing.json")])
    err = capsys.readouterr().err
    assert rc == 2
    assert "could not be loaded" in err


def test_template_with_unverified_runs_is_actionable(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("SPIDER_QWEN_STATE_DIR", str(tmp_path))
    # A run without the verification spine writes claim rows with no
    # verifier_score; the template must say what is missing.
    result = _run_cli(capsys, ["run", "office cleaning Singapore", "--offline"])
    rc = main(["calibrate", "template", result["run_id"], "--out", str(tmp_path / "c.json")])
    err = capsys.readouterr().err
    assert rc == 1
    assert "SPIDER_QWEN_VERIFICATION_ENABLED" in err


def test_run_metrics_include_latency_breakdown(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("SPIDER_QWEN_STATE_DIR", str(tmp_path))
    result = _run_cli(capsys, ["run", "office cleaning Singapore", "--offline"])
    latency = result["metrics"]["latency_seconds"]
    assert set(latency) == {"gather", "verify", "rfq", "total"}
    assert all(v >= 0 for v in latency.values())
    assert latency["total"] >= latency["gather"]
