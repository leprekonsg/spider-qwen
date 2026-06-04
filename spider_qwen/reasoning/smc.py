"""Deterministic SMC-style posterior over trajectory bundles.

The spec asked for particle inference, but this repo's reasoning layer is
deterministic and dependency-light. Treat each scored trajectory bundle as a
particle, update a uniform prior by the PPRM reward likelihood, and report the
posterior/ESS. No random resampling is needed for the small fixed trajectory set.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, Field

from .. import SCHEMA_VERSION
from .trajectory import TrajectoryBundle


class TrajectoryParticle(BaseModel):
    schema_version: str = SCHEMA_VERSION
    trajectory_id: str
    strategy: str
    reward: float
    posterior: float


class SmcSummary(BaseModel):
    schema_version: str = SCHEMA_VERSION
    particles: list[TrajectoryParticle] = Field(default_factory=list)
    effective_sample_size: float = 0.0
    winner_probability: float = 0.0
    abstain: bool = False
    rationale: str = ""


def infer_trajectory_posterior(
    bundles: list[TrajectoryBundle],
    *,
    temperature: float = 0.15,
    min_winner_probability: float = 0.55,
) -> SmcSummary:
    """Posterior over ranked trajectory bundles from PPRM rewards.

    Rewards are converted to likelihoods with a temperature-scaled softmax.
    A narrow posterior means the selected strategy is robust; a diffuse posterior
    means the runner should expose uncertainty rather than overstate the win.
    """
    scored = [b for b in bundles if b.reward is not None]
    if not scored:
        return SmcSummary(abstain=True, rationale="no scored trajectory particles")

    temp = max(float(temperature), 1e-6)
    rewards = [float(b.reward or 0.0) for b in scored]
    peak = max(rewards)
    weights = [math.exp((r - peak) / temp) for r in rewards]
    total = sum(weights)
    if total <= 0.0:
        return SmcSummary(abstain=True, rationale="trajectory particle weights collapsed")

    posteriors = [w / total for w in weights]
    ess = 1.0 / sum(p * p for p in posteriors)
    particles = [
        TrajectoryParticle(
            trajectory_id=b.trajectory.trajectory_id,
            strategy=b.trajectory.strategy.value,
            reward=round(float(b.reward or 0.0), 4),
            posterior=round(p, 6),
        )
        for b, p in zip(scored, posteriors)
    ]
    winner_probability = max(posteriors)
    abstain = winner_probability < min_winner_probability
    return SmcSummary(
        particles=particles,
        effective_sample_size=round(ess, 4),
        winner_probability=round(winner_probability, 6),
        abstain=abstain,
        rationale=(
            f"winner posterior {winner_probability:.3f} "
            f"{'below' if abstain else 'meets'} threshold {min_winner_probability:.2f}"
        ),
    )
