"""T-R.2: trajectory runner + recursive repair + winner selection (standalone).

The runner orchestrates >=2 strategy trajectories within budget via an injectable
executor (sync or async), refines evidence gaps in a bounded round 2, scores via
PPRM, selects the winner, and emits a why-it-won / why-alternates-lost explanation.
"""

from __future__ import annotations

import asyncio

from spider_qwen.reasoning.recursive_refiner import build_repair_queries, find_evidence_gaps
from spider_qwen.reasoning.trajectory import (
    BundleMetrics,
    ReasoningBudget,
    ReasoningTrajectory,
    TrajectoryBundle,
    TrajectoryStrategy,
)
from spider_qwen.reasoning.trajectory_runner import TrajectoryRunner, strategy_queries


def test_runner_runs_multiple_trajectories_and_selects_winner():
    def executor(traj: ReasoningTrajectory) -> TrajectoryBundle:
        m = BundleMetrics(
            service_match=0.8, geo=0.8, checklist=0.7, contact_reliability=0.6, evidence_diversity=0.5,
            quote_channel=1.0 if traj.strategy == TrajectoryStrategy.QUOTE_CHANNEL_FIRST else 0.0,
        )
        return TrajectoryBundle(trajectory=traj, metrics=m, searches_used=2, fetches_used=4, candidate_count=2)

    res = asyncio.run(TrajectoryRunner().run("office cleaning Singapore", "service_quote_required", executor=executor))
    assert len(res.bundles) >= 2
    assert res.winner.trajectory.strategy == TrajectoryStrategy.QUOTE_CHANNEL_FIRST
    assert "won" in res.explanation.lower()
    assert res.winner.trajectory.strategy.value in res.explanation
    assert res.within_budget is True
    assert res.smc is not None
    assert res.smc.particles
    assert res.smc.winner_probability > 0.0


def test_round2_repair_triggers_on_gap_and_merges_improvement():
    calls: list[int] = []

    def executor(traj: ReasoningTrajectory) -> TrajectoryBundle:
        calls.append(traj.round)
        if traj.round == 1:
            return TrajectoryBundle(trajectory=traj, searches_used=2, fetches_used=4,
                                    metrics=BundleMetrics(service_match=0.7, quote_channel=0.0, geo=0.7, checklist=0.7))
        return TrajectoryBundle(trajectory=traj, searches_used=1, fetches_used=2,
                                metrics=BundleMetrics(quote_channel=1.0))  # repair recovers the channel

    res = asyncio.run(
        TrajectoryRunner(budget=ReasoningBudget(max_trajectories=1, max_refinement_rounds=2))
        .run("office cleaning", "service_quote_required", executor=executor)
    )
    assert 2 in calls  # a repair round ran
    assert res.winner.metrics.quote_channel == 1.0  # merged improvement
    assert res.winner.rounds_used == 2


def test_runner_awaits_async_executor():
    async def executor(traj: ReasoningTrajectory) -> TrajectoryBundle:
        return TrajectoryBundle(trajectory=traj, metrics=BundleMetrics(service_match=1.0, quote_channel=1.0, geo=1.0,
                                checklist=1.0, contact_reliability=1.0, evidence_diversity=1.0),
                                searches_used=2, fetches_used=4)

    res = asyncio.run(TrajectoryRunner().run("q", "service_quote_required", executor=executor))
    assert res.winner is not None and res.winner.reward == 1.0


def test_find_gaps_and_build_repair_queries():
    traj = ReasoningTrajectory(strategy=TrajectoryStrategy.OFFICIAL_SITE_FIRST,
                               mode="service_quote_required", seed_query="office cleaning", queries=["q"])
    bundle = TrajectoryBundle(trajectory=traj, metrics=BundleMetrics(quote_channel=0.0, checklist=0.2, geo=0.0))
    gaps = find_evidence_gaps(bundle)
    assert {g.kind for g in gaps} >= {"quote_channel", "checklist", "geo"}
    repair = build_repair_queries(gaps, "office cleaning")
    assert repair and all("office cleaning" in q for q in repair)


def test_strategy_queries_are_bounded_and_strategy_shaped():
    qs = strategy_queries(TrajectoryStrategy.FFF_SUBSTITUTE_FIRST, "NE5532", "electronics_substitution", max_queries=2)
    assert qs and len(qs) <= 2
    assert any(any(t in q.lower() for t in ("substitute", "cross", "equivalent", "replacement")) for q in qs)


def test_repair_does_not_silently_clear_unresolved_disputes():
    """A round-1 dispute must survive a repair round that merely found no
    contradiction in its own (different) evidence subset. We model no
    dispute-resolution mechanism, so disputes are sticky: the merged bundle keeps
    disputed_count and conflict_penalty consistent (both monotonic via max), and
    never reports disputed=False while still carrying a conflict penalty."""

    def executor(traj: ReasoningTrajectory) -> TrajectoryBundle:
        if traj.round == 1:
            return TrajectoryBundle(
                trajectory=traj, searches_used=2, fetches_used=4,
                metrics=BundleMetrics(service_match=0.4, quote_channel=0.0, geo=0.4, checklist=0.4),
                disputed_count=1, conflict_penalty=0.4,
            )
        # Repair recovers the quote channel but does NOT resolve round 1's dispute.
        return TrajectoryBundle(
            trajectory=traj, searches_used=1, fetches_used=2,
            metrics=BundleMetrics(quote_channel=1.0), disputed_count=0, conflict_penalty=0.0,
        )

    res = asyncio.run(
        TrajectoryRunner(budget=ReasoningBudget(max_trajectories=1, max_refinement_rounds=2))
        .run("office cleaning", "service_quote_required", executor=executor)
    )
    assert res.winner.metrics.quote_channel == 1.0  # repair still merged its improvement
    assert res.winner.disputed_count == 1           # dispute not silently cleared
    assert res.winner.conflict_penalty == 0.4       # stays consistent with the dispute
    assert res.disputed is True


def test_smc_posterior_abstains_when_rewards_are_tied():
    def executor(traj: ReasoningTrajectory) -> TrajectoryBundle:
        return TrajectoryBundle(
            trajectory=traj,
            metrics=BundleMetrics(service_match=0.5, quote_channel=0.5, geo=0.5),
            searches_used=1,
            fetches_used=1,
        )

    res = asyncio.run(TrajectoryRunner().run("office cleaning", "service_quote_required", executor=executor))
    assert res.smc is not None
    assert res.smc.abstain is True
    assert res.smc.effective_sample_size > 1.0
    # The abstain signal is acted on, not just computed: the explanation
    # qualifies the winner instead of declaring an unqualified victory.
    assert "provisional" in res.explanation
