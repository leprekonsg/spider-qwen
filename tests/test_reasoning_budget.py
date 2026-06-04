"""T-R.1/T-R.2: reasoning budget invariants (Guardrail Gate).

The trajectory width/depth and per-trajectory search/fetch budgets are frozen and
the agent cannot self-raise them; activation is gated by mode + classifier
confidence. (T-R.2 extends this file with the runtime-enforcement test that the
runner never exceeds the derived caps.)
"""

from __future__ import annotations

import dataclasses

import pytest

from spider_qwen.reasoning.trajectory import ReasoningBudget, TrajectoryStrategy, strategies_for
from spider_qwen.reasoning.trajectory_policy import ReasoningConfig, should_activate


def test_budget_is_frozen_so_the_agent_cannot_self_raise_caps():
    budget = ReasoningBudget()
    with pytest.raises(dataclasses.FrozenInstanceError):
        budget.max_trajectories = 99  # type: ignore[misc]


def test_derived_total_caps_are_the_product_of_width_and_per_trajectory():
    budget = ReasoningBudget(max_trajectories=3, max_search_calls_per_trajectory=2, max_fetch_urls_per_trajectory=6)
    assert budget.total_search_cap == 6
    assert budget.total_fetch_cap == 18


def test_activation_requires_enabled_supported_mode_and_low_confidence():
    cfg = ReasoningConfig()  # threshold 0.75, default activate modes
    assert should_activate("service_quote_required", classifier_confidence=0.5, config=cfg) is True
    assert should_activate("service_quote_required", classifier_confidence=0.9, config=cfg) is False
    # contact_enrichment_only is intentionally NOT a high-value trajectory mode.
    assert should_activate("contact_enrichment_only", classifier_confidence=0.1, config=cfg) is False
    disabled = dataclasses.replace(cfg, enabled=False)
    assert should_activate("service_quote_required", classifier_confidence=0.1, config=disabled) is False


def test_strategy_sets_are_distinct_per_vertical():
    service = strategies_for("service_quote_required")
    electronics = strategies_for("electronics_substitution")
    assert len(service) == 5 and len(set(service)) == 5
    assert len(electronics) == 5 and len(set(electronics)) == 5
    assert set(service).isdisjoint(set(electronics))
    assert strategies_for("product_exact_price") == electronics
    assert TrajectoryStrategy.QUOTE_CHANNEL_FIRST in service
    assert TrajectoryStrategy.FFF_SUBSTITUTE_FIRST in electronics


def test_ceilings_include_refinement_rounds():
    budget = ReasoningBudget()  # 3 traj x 2 search x 6 fetch x 2 rounds
    assert budget.search_ceiling == 12
    assert budget.fetch_ceiling == 36


def test_runner_never_exceeds_ceilings_even_with_a_greedy_executor():
    import asyncio

    from spider_qwen.reasoning.trajectory import TrajectoryBundle, BundleMetrics
    from spider_qwen.reasoning.trajectory_runner import TrajectoryRunner

    budget = ReasoningBudget()

    def greedy(traj):
        # Claims to use far more than allowed; the runner must clamp per round.
        return TrajectoryBundle(trajectory=traj, metrics=BundleMetrics(service_match=1.0),
                                searches_used=99, fetches_used=99)

    res = asyncio.run(TrajectoryRunner(budget=budget).run("q", "service_quote_required", executor=greedy))
    assert res.total_searches <= budget.search_ceiling
    assert res.total_fetches <= budget.fetch_ceiling
    assert len(res.bundles) <= budget.max_trajectories
    assert res.within_budget is True
