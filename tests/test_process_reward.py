"""T-R.1: deterministic Procurement Process Reward Model (PPRM).

Scores a whole trajectory *bundle* (not a page) into one comparable reward, so the
runner can pick the best-evidenced sourcing strategy. Transparent weighted sum +
explicit penalties; no neural training.
"""

from __future__ import annotations

import pytest

from spider_qwen.reasoning.process_reward import (
    ELECTRONICS_WEIGHTS,
    SERVICE_WEIGHTS,
    ProcessReward,
    weights_for,
)
from spider_qwen.reasoning.trajectory import (
    BundleMetrics,
    ReasoningTrajectory,
    TrajectoryBundle,
    TrajectoryStrategy,
)


def _bundle(mode, strategy, metrics, **penalties):
    traj = ReasoningTrajectory(strategy=strategy, mode=mode, seed_query="q", queries=["q"])
    return TrajectoryBundle(trajectory=traj, metrics=BundleMetrics(**metrics), **penalties)


def test_weight_sets_are_normalised():
    assert round(sum(SERVICE_WEIGHTS.values()), 6) == 1.0
    assert round(sum(ELECTRONICS_WEIGHTS.values()), 6) == 1.0


def test_perfect_service_bundle_scores_one():
    b = _bundle(
        "service_quote_required", TrajectoryStrategy.QUOTE_CHANNEL_FIRST,
        dict(service_match=1, quote_channel=1, geo=1, contact_reliability=1, checklist=1, evidence_diversity=1),
    )
    reward, breakdown = ProcessReward().score(b)
    assert reward == 1.0
    assert round(sum(v for v in breakdown.values()), 4) == 1.0


def test_missing_quote_channel_lowers_service_reward_by_its_weight():
    full = _bundle("service_quote_required", TrajectoryStrategy.QUOTE_CHANNEL_FIRST,
                   dict(service_match=1, quote_channel=1, geo=1, contact_reliability=1, checklist=1, evidence_diversity=1))
    no_qc = _bundle("service_quote_required", TrajectoryStrategy.OFFICIAL_SITE_FIRST,
                    dict(service_match=1, quote_channel=0, geo=1, contact_reliability=1, checklist=1, evidence_diversity=1))
    r_full, _ = ProcessReward().score(full)
    r_noqc, _ = ProcessReward().score(no_qc)
    assert round(r_full - r_noqc, 4) == SERVICE_WEIGHTS["quote_channel"]


def test_penalties_subtract_from_reward():
    b = _bundle("service_quote_required", TrajectoryStrategy.QUOTE_CHANNEL_FIRST,
                dict(service_match=1, quote_channel=1, geo=1, contact_reliability=1, checklist=1, evidence_diversity=1),
                conflict_penalty=0.2, budget_penalty=0.1)
    reward, breakdown = ProcessReward().score(b)
    assert reward == round(1.0 - 0.3, 4)
    assert breakdown["conflict_penalty"] == -0.2 and breakdown["budget_penalty"] == -0.1


def test_electronics_uses_electronics_weights_and_counterfeit_penalty():
    assert weights_for("electronics_substitution") is ELECTRONICS_WEIGHTS
    assert weights_for("product_exact_price") is ELECTRONICS_WEIGHTS
    assert weights_for("service_quote_required") is SERVICE_WEIGHTS

    b = _bundle("electronics_substitution", TrajectoryStrategy.FFF_SUBSTITUTE_FIRST,
                dict(fff_similarity=1, lifecycle_safety=1, authorized_source=1, stock=1, datasheet_evidence=1, risk=1),
                counterfeit_penalty=0.4)
    reward, breakdown = ProcessReward().score(b)
    assert reward == round(1.0 - 0.4, 4)
    assert breakdown["counterfeit_penalty"] == -0.4


def test_score_all_sets_reward_and_ranks_winner_first():
    weak = _bundle("service_quote_required", TrajectoryStrategy.OFFICIAL_SITE_FIRST,
                   dict(service_match=0.5, quote_channel=0, geo=0.5))
    strong = _bundle("service_quote_required", TrajectoryStrategy.QUOTE_CHANNEL_FIRST,
                     dict(service_match=1, quote_channel=1, geo=1, contact_reliability=1, checklist=1, evidence_diversity=1))
    ranked = ProcessReward().score_all([weak, strong])
    assert ranked[0].trajectory.strategy == TrajectoryStrategy.QUOTE_CHANNEL_FIRST
    assert all(b.reward is not None for b in ranked)
    assert ranked[0].reward >= ranked[1].reward
