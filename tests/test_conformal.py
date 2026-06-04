"""Conformal abstention refuses fake guarantees without calibration data."""

from __future__ import annotations

from spider_qwen.verification.conformal import CalibrationExample, ConformalAbstainer


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
