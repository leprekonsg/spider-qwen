"""Conformal abstention refuses fake guarantees without calibration data."""

from __future__ import annotations

import asyncio
import json

import pytest

from spider_qwen.verification.conformal import (
    AbstentionDecision,
    CalibrationExample,
    ConformalAbstainer,
    abstainer_from_env,
)


def test_uncalibrated_abstainer_refuses_to_claim_guarantee():
    decision = ConformalAbstainer().decide(0.99)
    assert decision.abstain is True
    assert decision.calibrated is False
    assert "calibration" in decision.rationale


def test_fit_refuses_when_sample_too_small_for_alpha():
    # alpha=0.1 needs >= 9 correct examples: ceil((n+1)*0.9) <= n fails below
    # that, the conformal quantile is +infinity, and a clamped threshold would
    # silently claim coverage n/(n+1) < 1-alpha.
    abstainer = ConformalAbstainer.fit(
        [CalibrationExample(verifier_score=s, prediction_correct=True)
         for s in (0.95, 0.9, 0.8)],
        alpha=0.1,
    )
    assert abstainer.threshold is None
    assert any("at least 9" in r for r in abstainer.reasons)
    decision = abstainer.decide(0.99)
    assert decision.abstain is True
    assert decision.calibrated is False
    assert "at least 9" in decision.rationale


def test_calibrated_abstainer_accepts_only_scores_above_threshold():
    abstainer = ConformalAbstainer.fit(
        [
            CalibrationExample(verifier_score=0.95, prediction_correct=True),
            CalibrationExample(verifier_score=0.9, prediction_correct=True),
            CalibrationExample(verifier_score=0.8, prediction_correct=True),
            CalibrationExample(verifier_score=0.4, prediction_correct=False),
        ],
        alpha=0.25,
    )
    assert abstainer.threshold is not None
    assert abstainer.decide(0.95).abstain is False
    assert abstainer.decide(0.7).abstain is True


# --- calibration loading (SPIDER_QWEN_CONFORMAL_CALIBRATION) -----------------

def test_abstainer_from_env_unset_is_uncalibrated(monkeypatch):
    monkeypatch.delenv("SPIDER_QWEN_CONFORMAL_CALIBRATION", raising=False)
    abstainer = abstainer_from_env()
    assert abstainer.threshold is None
    assert "SPIDER_QWEN_CONFORMAL_CALIBRATION" in abstainer.reasons[0]


def test_abstainer_from_env_loads_calibration_file(tmp_path, monkeypatch):
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps({
        "alpha": 0.25,
        "examples": [{"verifier_score": s, "prediction_correct": True}
                     for s in (0.9, 0.8, 0.7)],
    }), encoding="utf-8")
    monkeypatch.setenv("SPIDER_QWEN_CONFORMAL_CALIBRATION", str(path))
    abstainer = abstainer_from_env()
    assert abstainer.alpha == 0.25
    # nonconformities [0.1, 0.2, 0.3], rank ceil(4*0.75)=3 -> threshold 1-0.3
    assert abstainer.threshold == 0.7


def test_abstainer_from_env_malformed_fails_loud(tmp_path, monkeypatch):
    path = tmp_path / "calibration.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("SPIDER_QWEN_CONFORMAL_CALIBRATION", str(path))
    with pytest.raises(ValueError, match="SPIDER_QWEN_CONFORMAL_CALIBRATION"):
        abstainer_from_env()
    monkeypatch.setenv("SPIDER_QWEN_CONFORMAL_CALIBRATION", str(tmp_path / "missing.json"))
    with pytest.raises(ValueError, match="could not be loaded"):
        abstainer_from_env()


# --- emission gate wiring (controller) ---------------------------------------

class _AbstainAll:
    """Calibrated abstainer double that abstains on every score."""

    threshold = 0.99
    alpha = 0.1
    calibration_size = 20
    reasons: list[str] = []

    def decide(self, verifier_score: float) -> AbstentionDecision:
        return AbstentionDecision(
            abstain=True, calibrated=True, threshold=self.threshold,
            confidence=verifier_score, alpha=self.alpha, rationale="forced abstention",
        )


def test_calibrated_abstention_gates_candidate_emission(monkeypatch):
    from spider_qwen.agent.controller import Controller

    monkeypatch.delenv("SPIDER_QWEN_CONFORMAL_CALIBRATION", raising=False)
    controller = Controller(offline=True, verify=True, state_dir=None, persist=False,
                            conformal=_AbstainAll())
    result = asyncio.run(controller.run("office cleaning Singapore", mode="auto"))
    conformal = result.metrics["conformal"]
    assert conformal["calibrated"] is True
    assert conformal["candidates_abstained"] >= 1
    assert result.validated_candidates == []  # gated, so no RFQs either
    assert result.rfq_drafts == []


def test_uncalibrated_abstainer_never_gates_and_states_why(monkeypatch):
    from spider_qwen.agent.controller import Controller

    monkeypatch.delenv("SPIDER_QWEN_CONFORMAL_CALIBRATION", raising=False)
    controller = Controller(offline=True, verify=True, state_dir=None, persist=False)
    result = asyncio.run(controller.run("office cleaning Singapore", mode="auto"))
    conformal = result.metrics["conformal"]
    assert conformal["calibrated"] is False
    assert conformal["candidates_abstained"] == 0
    assert "SPIDER_QWEN_CONFORMAL_CALIBRATION" in conformal["rationale"]
    assert result.validated_candidates  # verification alone decides
