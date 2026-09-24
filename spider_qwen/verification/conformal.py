"""Statistical gates over verifier scores: selective risk (gates) + coverage (advisory).

Two calibrated objects with one honest split of duties:

- ``SelectiveRiskGate`` -- the emission gate. Learn-then-Test (Angelopoulos et
  al., arXiv:2110.01052) with exact binomial tail p-values over a fixed,
  data-independent threshold grid under Bonferroni correction (selective-risk
  target as in SGR, Geifman & El-Yaniv, arXiv:1705.08500). Guarantee: with
  probability >= 1-delta over the calibration draw, P(wrong | emitted) <= alpha
  for candidates exchangeable with the calibration set. This is the risk an
  emission gate must control -- it bounds confident-but-wrong emissions.

- ``ConformalAbstainer`` -- split-conformal coverage, advisory only. Bounds
  false abstention on CORRECT predictions at alpha ("we rarely drop a good
  vendor"). It places no bound on wrong-but-accepted candidates and therefore
  must not gate emission; it remains available as a recall diagnostic and for
  the CLI calibration harness.

No calibration set, no guarantee. Both objects make that explicit: an
uncalibrated instance states the missing prerequisite instead of fabricating a
guarantee, and (in the controller) never gates.
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
    delta: float | None = None  # set by SelectiveRiskGate: certificate confidence 1-delta
    rationale: str = ""


def _binom_cdf(k: int, m: int, p: float) -> float:
    """P[X <= k] for X ~ Binomial(m, p): the exact LTT p-value for
    H: selective risk > p, given k wrong among m emitted calibration points."""
    return min(1.0, sum(math.comb(m, i) * p**i * (1.0 - p) ** (m - i) for i in range(k + 1)))


# Candidate emission thresholds: a FIXED, data-independent policy grid. Testing
# a small fixed grid under Bonferroni keeps the multiplicity cost constant and
# needs no ordering assumption (a fixed-sequence walk from the strictest
# threshold has one emitted example at the top and can never reject; a walk
# from the loosest dies on its first failure). Denser grids buy resolution at
# the price of power: each point is tested at delta / len(grid).
_LTT_GRID: tuple[float, ...] = (0.5, 0.75, 0.9)


class SelectiveRiskGate(BaseModel):
    """LTT emission gate: emit iff verifier_score >= threshold.

    Guarantee when calibrated: with probability >= 1-delta over the calibration
    draw, P(wrong | emitted) <= alpha -- provided future candidates are
    exchangeable with the calibration candidates (same pipeline version, same
    query mix) and "wrong" means what the labeling procedure measured. Marginal,
    not per-candidate; recalibrate on any pipeline-version change.
    """

    schema_version: str = SCHEMA_VERSION
    alpha: float = 0.1  # target wrong-emission rate among accepted candidates
    delta: float = 0.1  # certificate confidence is 1 - delta
    threshold: float | None = None
    calibration_size: int = 0  # total labeled examples seen
    calibration_emitted: int = 0  # m: examples at/above the chosen threshold
    calibration_wrong: int = 0  # k: wrong among those m
    reasons: list[str] = Field(default_factory=list)

    @classmethod
    def fit(cls, examples: list[CalibrationExample], *, alpha: float = 0.1,
            delta: float = 0.1) -> "SelectiveRiskGate":
        """LTT over the fixed threshold grid, Bonferroni at delta/len(grid).

        For each grid threshold lambda, test H: P(wrong | score >= lambda) >
        alpha with the exact binomial tail p-value BinomCDF(k; m, alpha).
        Bonferroni bounds the family-wise error at delta, so every rejected
        threshold is simultaneously valid; the gate deploys the SMALLEST
        rejected lambda (the most permissive certified gate).

        Unlike the coverage abstainer, this needs candidates of BOTH classes
        (or at least enough error-free emitted mass): with zero observed
        errors, certifying alpha/delta over a 3-point grid requires
        m >= ln(delta/3)/ln(1-alpha) emitted calibration points
        (33 at 0.1/0.1, 80 at 0.05/0.05).
        """
        a = min(0.5, max(0.01, float(alpha)))
        d = min(0.5, max(0.01, float(delta)))
        if not examples:
            return cls(alpha=a, delta=d, reasons=["no labeled calibration examples"])
        scored = [(_clamp(e.verifier_score), bool(e.prediction_correct)) for e in examples]
        d_each = d / len(_LTT_GRID)
        for lam in sorted(_LTT_GRID):  # ascending: first certified = most permissive
            m = sum(1 for s, _ in scored if s >= lam)
            k = sum(1 for s, ok in scored if s >= lam and not ok)
            if m and _binom_cdf(k, m, a) <= d_each:
                return cls(alpha=a, delta=d, threshold=round(lam, 6),
                           calibration_size=len(examples),
                           calibration_emitted=m, calibration_wrong=k)
        floor = math.ceil(math.log(d_each) / math.log(1.0 - a))
        return cls(alpha=a, delta=d, calibration_size=len(examples), reasons=[
            f"insufficient calibration data for alpha={a:g}, delta={d:g}: no grid "
            f"threshold {sorted(_LTT_GRID)} certifies P(wrong|emitted) <= {a:g} "
            f"(with zero observed errors this needs >= {floor} emitted calibration "
            f"examples at some threshold)"
        ])

    def decide(self, verifier_score: float) -> AbstentionDecision:
        confidence = _clamp(verifier_score)
        if self.threshold is None:
            return AbstentionDecision(
                abstain=True, calibrated=False, confidence=confidence,
                alpha=self.alpha, delta=self.delta,
                rationale="; ".join(self.reasons)
                or "no labeled calibration set; selective-risk guarantee unavailable",
            )
        abstain = confidence < self.threshold
        return AbstentionDecision(
            abstain=abstain, calibrated=True, threshold=self.threshold,
            confidence=confidence, alpha=self.alpha, delta=self.delta,
            rationale=(
                f"score {confidence:.3f} {'below' if abstain else 'meets'} LTT "
                f"threshold {self.threshold:.3f}: P(wrong|emitted) <= {self.alpha:g} "
                f"with confidence {1.0 - self.delta:g}"
            ),
        )


class ConformalAbstainer(BaseModel):
    schema_version: str = SCHEMA_VERSION
    alpha: float = 0.1
    threshold: float | None = None
    calibration_size: int = 0
    # Only the correct examples define the coverage guarantee; surfacing the
    # count separately keeps "calibrated on N examples" honest.
    calibration_correct: int = 0
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
                calibration_correct=len(correct),
                reasons=[
                    f"need at least {needed} correct calibration examples for "
                    f"alpha={clean_alpha:g} (have {len(correct)})"
                ],
            )
        return cls(
            alpha=clean_alpha,
            threshold=round(1.0 - correct[rank - 1], 6),
            calibration_size=len(examples),
            calibration_correct=len(correct),
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


_NO_CALIBRATION_REASON = (
    "no calibration set configured (set SPIDER_QWEN_CONFORMAL_CALIBRATION "
    "to a JSON file of hand-graded examples)"
)


def _load_calibration(path: str) -> tuple[list[CalibrationExample], dict]:
    """Load hand-graded examples; a malformed file raises ValueError naming the
    file and the expected shape -- misconfiguration must not silently degrade
    to "no guarantee"."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        examples = [CalibrationExample.model_validate(e) for e in payload["examples"]]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(
            f"SPIDER_QWEN_CONFORMAL_CALIBRATION={path!r} could not be loaded: {exc}. "
            'Expected a JSON file like {"alpha": 0.1, "delta": 0.1, "examples": '
            '[{"verifier_score": 0.9, "prediction_correct": true}, ...]}'
        ) from exc
    return examples, payload


def gate_from_env(*, expected_pipeline_version: str | None = None,
                  expected_config_fingerprint: str | None = None) -> SelectiveRiskGate:
    """Build the run's EMISSION GATE from SPIDER_QWEN_CONFORMAL_CALIBRATION.

    Same file as the coverage abstainer -- ``examples`` with correct/incorrect
    labels -- plus optional ``alpha`` (wrong-emission target) and ``delta``
    (certificate confidence). Unset: an uncalibrated gate whose decisions state
    that no guarantee is available (it never gates).
    """
    path = os.getenv("SPIDER_QWEN_CONFORMAL_CALIBRATION", "").strip()
    if not path:
        return SelectiveRiskGate(reasons=[
            _NO_CALIBRATION_REASON + "; selective-risk guarantee unavailable"
        ])
    examples, payload = _load_calibration(path)
    if expected_pipeline_version is None:
        from ..application.profiles import PIPELINE_VERSION
        expected_pipeline_version = PIPELINE_VERSION
    if payload.get("pipeline_version") != expected_pipeline_version:
        return SelectiveRiskGate(reasons=[
            "Calibration pipeline_version is missing or differs from the active pipeline; "
            f"collect and hand-grade new evaluations for {expected_pipeline_version}. "
            "Selective-risk guarantee unavailable."
        ])
    if expected_config_fingerprint is None:
        # Fail closed: without this run's fingerprint there is no way to show
        # the file was graded under the same providers, models and policy.
        return SelectiveRiskGate(reasons=[
            "This run has no config_fingerprint (plain `spider-qwen run`), so the "
            "calibration file cannot be matched to its providers, models and policy; "
            "use an operator profile run (run service or `benchmark --profile`). "
            "Selective-risk guarantee unavailable."
        ])
    if payload.get("config_fingerprint") != expected_config_fingerprint:
        return SelectiveRiskGate(reasons=[
            "Calibration config_fingerprint is missing or differs from this run's configuration; "
            "collect and hand-grade evaluations with the same providers, models and policy. "
            "Selective-risk guarantee unavailable."
        ])
    return SelectiveRiskGate.fit(
        examples,
        alpha=float(payload.get("alpha", 0.1)),
        delta=float(payload.get("delta", 0.1)),
    )


def abstainer_from_env() -> ConformalAbstainer:
    """Build the ADVISORY coverage abstainer from SPIDER_QWEN_CONFORMAL_CALIBRATION.

    The env var names a JSON file of hand-graded examples:
    ``{"alpha": 0.1, "examples": [{"verifier_score": 0.9, "prediction_correct": true}, ...]}``.
    Unset: an uncalibrated abstainer whose decisions state that no guarantee is
    available. Coverage bounds false abstention on correct predictions only; it
    must not be deployed as an emission gate (use ``gate_from_env``).
    """
    path = os.getenv("SPIDER_QWEN_CONFORMAL_CALIBRATION", "").strip()
    if not path:
        return ConformalAbstainer(reasons=[
            _NO_CALIBRATION_REASON + "; conformal guarantee unavailable"
        ])
    examples, payload = _load_calibration(path)
    return ConformalAbstainer.fit(examples, alpha=float(payload.get("alpha", 0.1)))


def _clamp(value: float) -> float:
    return round(max(0.0, min(1.0, float(value))), 6)
