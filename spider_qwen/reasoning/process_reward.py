"""T-R.1: deterministic Procurement Process Reward Model (PPRM).

The GRAM-style selection layer, translated to a transparent weighted score over a
trajectory bundle's normalized sub-scores minus explicit penalties. No neural
training. Service and electronics each have a weight set that sums to 1.0, so a
perfect bundle scores 1.0 before penalties. Weights are overridable (a config seam
for per-demo tuning) but default to module constants.
"""

from __future__ import annotations

from .trajectory import TrajectoryBundle

SERVICE_WEIGHTS: dict[str, float] = {
    "service_match": 0.25,
    "quote_channel": 0.25,
    "geo": 0.20,
    "contact_reliability": 0.15,
    "checklist": 0.10,
    "evidence_diversity": 0.05,
}
ELECTRONICS_WEIGHTS: dict[str, float] = {
    "fff_similarity": 0.30,
    "lifecycle_safety": 0.20,
    "authorized_source": 0.15,
    "stock": 0.15,
    "datasheet_evidence": 0.10,
    "risk": 0.10,
}

_SERVICE_MODES = {"service_quote_required", "contact_enrichment_only"}


def weights_for(mode: str) -> dict[str, float]:
    return SERVICE_WEIGHTS if mode in _SERVICE_MODES else ELECTRONICS_WEIGHTS


class ProcessReward:
    def __init__(
        self,
        service_weights: dict[str, float] | None = None,
        electronics_weights: dict[str, float] | None = None,
    ) -> None:
        self._service = service_weights or SERVICE_WEIGHTS
        self._electronics = electronics_weights or ELECTRONICS_WEIGHTS

    def _weights_for(self, mode: str) -> dict[str, float]:
        return self._service if mode in _SERVICE_MODES else self._electronics

    def score(self, bundle: TrajectoryBundle) -> tuple[float, dict[str, float]]:
        weights = self._weights_for(bundle.trajectory.mode)
        is_electronics = weights is self._electronics
        breakdown = {k: round(w * getattr(bundle.metrics, k), 4) for k, w in weights.items()}
        base = sum(breakdown.values())

        penalties = {
            "conflict_penalty": bundle.conflict_penalty,
            "budget_penalty": bundle.budget_penalty,
        }
        if is_electronics:
            penalties["counterfeit_penalty"] = bundle.counterfeit_penalty
        for name, value in penalties.items():
            breakdown[name] = -round(value, 4)

        reward = round(base - sum(penalties.values()), 4)
        return reward, breakdown

    def score_all(self, bundles: list[TrajectoryBundle]) -> list[TrajectoryBundle]:
        """Score each bundle in place and return them ranked best-first."""
        for bundle in bundles:
            bundle.reward, bundle.reward_breakdown = self.score(bundle)
        return sorted(bundles, key=lambda b: b.reward if b.reward is not None else float("-inf"), reverse=True)
