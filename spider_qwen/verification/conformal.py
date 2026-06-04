"""Conformal abstention for verifier scores.

No calibration set, no guarantee. This module makes that explicit: callers can
ask for an abstention decision, but an uncalibrated instance always abstains and
states the missing prerequisite instead of fabricating a coverage claim.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

from pydantic import BaseModel, Field

from .. import SCHEMA_VERSION


class CalibrationExample(BaseModel):
    verifier_score: float
    prediction_correct: bool


class AbstentionDecision(BaseModel):
    schema_version: str = SCHEMA_VERSION
    abstain: bool
    calibrated: bool
    threshold: float | None = None
    confidence: float
    alpha: float
    rationale: str = ""


class ConformalAbstainer(BaseModel):
    schema_version: str = SCHEMA_VERSION
    alpha: float = 0.1
    threshold: float | None = None
    calibration_size: int = 0
    reasons: list[str] = Field(default_factory=list)

    @classmethod
    def fit(cls, examples: list[CalibrationExample], *, alpha: float = 0.1) -> "ConformalAbstainer":
        """Fit an abstention threshold from hand-graded calibration examples.

        We calibrate on nonconformity ``1 - verifier_score`` for examples where
        the verifier prediction was correct. Accepting a new prediction requires
        nonconformity <= the conformal quantile. Incorrect calibration examples
        are retained in the size count but do not define the correctness region.

        The guarantee this carries: at most ``alpha`` of correct predictions are
        falsely abstained on (exchangeability assumed). It does NOT bound the
        error rate among accepted predictions -- do not read the threshold as
        selective risk control.
        """
        clean_alpha = min(0.99, max(0.01, float(alpha)))
        correct = [1.0 - _clamp(e.verifier_score) for e in examples if e.prediction_correct]
        if not correct:
            return cls(
                alpha=clean_alpha,
                calibration_size=len(examples),
                reasons=["no correct hand-graded calibration examples"],
            )
        correct.sort()
        # Split-conformal finite-sample quantile: ceil((n + 1) * (1 - alpha)).
        # When the rank lands past the sample, the conformal quantile is
        # +infinity: no finite threshold carries the guarantee. Refuse rather
        # than silently substituting the max nonconformity, which would claim
        # coverage n/(n+1) < 1-alpha.
        rank = math.ceil((len(correct) + 1) * (1.0 - clean_alpha))
        if rank > len(correct):
            needed = math.ceil((1.0 - clean_alpha) / clean_alpha)
            return cls(
                alpha=clean_alpha,
                calibration_size=len(examples),
                reasons=[
                    f"need at least {needed} correct calibration examples for "
                    f"alpha={clean_alpha:g} (have {len(correct)})"
                ],
            )
        return cls(
            alpha=clean_alpha,
            threshold=round(1.0 - correct[rank - 1], 6),
            calibration_size=len(examples),
        )

    def decide(self, verifier_score: float) -> AbstentionDecision:
        confidence = _clamp(verifier_score)
        if self.threshold is None:
            return AbstentionDecision(
                abstain=True,
                calibrated=False,
                confidence=confidence,
                alpha=self.alpha,
                rationale="; ".join(self.reasons)
                or "no hand-graded calibration set; conformal guarantee unavailable",
            )
        abstain = confidence < self.threshold
        return AbstentionDecision(
            abstain=abstain,
            calibrated=True,
            threshold=self.threshold,
            confidence=confidence,
            alpha=self.alpha,
            rationale=(
                f"score {confidence:.3f} "
                f"{'below' if abstain else 'meets'} calibrated threshold {self.threshold:.3f}"
            ),
        )


def abstainer_from_env() -> ConformalAbstainer:
    """Build the run's abstainer from SPIDER_QWEN_CONFORMAL_CALIBRATION.

    The env var names a JSON file of hand-graded examples:
    ``{"alpha": 0.1, "examples": [{"verifier_score": 0.9, "prediction_correct": true}, ...]}``.
    Unset: an uncalibrated abstainer whose decisions state that no guarantee is
    available (it never gates). A malformed file raises ValueError naming the
    file and the expected shape -- misconfiguration must not silently degrade
    to "no guarantee".
    """
    path = os.getenv("SPIDER_QWEN_CONFORMAL_CALIBRATION", "").strip()
    if not path:
        return ConformalAbstainer(reasons=[
            "no calibration set configured (set SPIDER_QWEN_CONFORMAL_CALIBRATION "
            "to a JSON file of hand-graded examples); conformal guarantee unavailable"
        ])
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        examples = [CalibrationExample.model_validate(e) for e in payload["examples"]]
        alpha = float(payload.get("alpha", 0.1))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(
            f"SPIDER_QWEN_CONFORMAL_CALIBRATION={path!r} could not be loaded: {exc}. "
            'Expected a JSON file like {"alpha": 0.1, "examples": '
            '[{"verifier_score": 0.9, "prediction_correct": true}, ...]}'
        ) from exc
    return ConformalAbstainer.fit(examples, alpha=alpha)


def _clamp(value: float) -> float:
    return round(max(0.0, min(1.0, float(value))), 6)
