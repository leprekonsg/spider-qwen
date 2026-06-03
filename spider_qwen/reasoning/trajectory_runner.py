"""T-R.2: trajectory runner -- orchestrate, refine, score, select the winner.

Runs up to ``max_trajectories`` strategy trajectories through an injectable
executor (sync or async -- the controller supplies a real search/fetch/extract
executor; tests supply deterministic stubs), refines evidence gaps in a bounded
round 2, scores each bundle with the Process Reward Model, and selects the
best-evidenced bundle. The result explains why the winner won and why the
alternates lost. Budgets are clamped per round so totals can never exceed the
budget ceilings.
"""

from __future__ import annotations

import inspect
from typing import Awaitable, Callable, Union

from pydantic import BaseModel, Field

from .. import SCHEMA_VERSION
from .process_reward import ProcessReward
from .recursive_refiner import build_repair_queries, find_evidence_gaps
from .trajectory import (
    BundleMetrics,
    ReasoningBudget,
    ReasoningTrajectory,
    TrajectoryBundle,
    TrajectoryStrategy,
    strategies_for,
)

Executor = Callable[[ReasoningTrajectory], Union[TrajectoryBundle, Awaitable[TrajectoryBundle]]]

# One or two strategy-shaped search queries per strategy (obsolescence vocab baked
# into the electronics strategies). Bounded by the per-trajectory search cap.
_STRATEGY_QUERIES: dict[TrajectoryStrategy, list[str]] = {
    TrajectoryStrategy.OFFICIAL_SITE_FIRST: ["{q} official site"],
    TrajectoryStrategy.QUOTE_CHANNEL_FIRST: ["{q} request a quote", "{q} contact sales RFQ"],
    TrajectoryStrategy.DIRECTORY_CONTACT_FIRST: ["{q} directory listing", "{q} contact"],
    TrajectoryStrategy.SEA_LOCAL_PHRASING_FIRST: ["{q} Singapore Malaysia supplier pembekal"],
    TrajectoryStrategy.CATEGORY_PRICING_FIRST: ["{q} price rate card"],
    TrajectoryStrategy.AUTHORIZED_DISTRIBUTOR_FIRST: ["{q} authorized distributor", "{q} Mouser Digi-Key"],
    TrajectoryStrategy.FFF_SUBSTITUTE_FIRST: ["{q} substitute cross reference", "{q} equivalent replacement"],
    TrajectoryStrategy.LIFECYCLE_PCN_FIRST: ["{q} EOL PCN lifecycle NRND"],
    TrajectoryStrategy.BROKER_LONG_TAIL_FIRST: ["{q} broker stock surplus obsolete"],
    TrajectoryStrategy.ARCHIVED_DATASHEET_FIRST: ["{q} datasheet archive"],
}


def strategy_queries(strategy: TrajectoryStrategy, seed_query: str, mode: str, max_queries: int = 2) -> list[str]:
    templates = _STRATEGY_QUERIES.get(strategy, ["{q}"])
    return [t.format(q=seed_query) for t in templates][:max_queries]


class ReasoningResult(BaseModel):
    schema_version: str = SCHEMA_VERSION
    query: str
    mode: str
    winner: TrajectoryBundle | None = None
    bundles: list[TrajectoryBundle] = Field(default_factory=list)  # ranked best-first
    explanation: str = ""
    total_searches: int = 0
    total_fetches: int = 0
    within_budget: bool = True
    disputed: bool = False


def _merge(base: TrajectoryBundle, repair: TrajectoryBundle) -> TrajectoryBundle:
    """Fold a repair-round bundle into the base: metrics improve (max), evidence
    unions, rounds advance. Repair never lowers a sub-score."""
    merged = base.metrics.model_dump()
    for key, value in repair.metrics.model_dump().items():
        if isinstance(value, (int, float)):
            merged[key] = max(merged.get(key, 0.0), value)
    base.metrics = BundleMetrics(**merged)

    seen = {r.ledger_id for r in base.evidence_refs}
    for ref in repair.evidence_refs:
        if ref.ledger_id not in seen:
            seen.add(ref.ledger_id)
            base.evidence_refs.append(ref)

    base.candidate_count += repair.candidate_count
    base.disputed_count = repair.disputed_count  # repair re-checks contradictions
    base.conflict_penalty = max(base.conflict_penalty, repair.conflict_penalty)
    base.counterfeit_penalty = max(base.counterfeit_penalty, repair.counterfeit_penalty)
    base.rounds_used = repair.trajectory.round
    return base


def _explain(ranked: list[TrajectoryBundle]) -> str:
    if not ranked:
        return "No trajectory produced an evidenced bundle."
    winner = ranked[0]
    positives = sorted(((k, v) for k, v in winner.reward_breakdown.items() if v > 0), key=lambda x: -x[1])[:2]
    won_on = ", ".join(f"{k} {v}" for k, v in positives) or "evidence"
    parts = [f"Winner {winner.trajectory.strategy.value} (reward {winner.reward}) won on {won_on}."]
    for loser in ranked[1:3]:
        weakest = sorted(loser.reward_breakdown.items(), key=lambda x: x[1])[:2]
        lost_on = ", ".join(f"{k} {v}" for k, v in weakest)
        parts.append(f"{loser.trajectory.strategy.value} lost (reward {loser.reward}): weakest {lost_on}.")
    if winner.disputed_count:
        parts.append(f"Winner carries {winner.disputed_count} disputed claim(s) flagged for review.")
    return " ".join(parts)


class TrajectoryRunner:
    def __init__(self, *, budget: ReasoningBudget | None = None, reward: ProcessReward | None = None) -> None:
        self.budget = budget or ReasoningBudget()
        self.reward = reward or ProcessReward()

    def _trajectories(self, query: str, mode: str) -> list[ReasoningTrajectory]:
        strategies = strategies_for(mode)[: self.budget.max_trajectories]
        return [
            ReasoningTrajectory(
                strategy=s, mode=mode, seed_query=query,
                queries=strategy_queries(s, query, mode, self.budget.max_search_calls_per_trajectory),
            )
            for s in strategies
        ]

    def _clamp(self, bundle: TrajectoryBundle) -> TrajectoryBundle:
        bundle.searches_used = min(bundle.searches_used, self.budget.max_search_calls_per_trajectory)
        bundle.fetches_used = min(bundle.fetches_used, self.budget.max_fetch_urls_per_trajectory)
        return bundle

    async def _call(self, executor: Executor, traj: ReasoningTrajectory) -> TrajectoryBundle:
        result = executor(traj)
        if inspect.isawaitable(result):
            result = await result
        return self._clamp(result)

    async def run(
        self,
        query: str,
        mode: str,
        *,
        executor: Executor,
        refiner_executor: Executor | None = None,
    ) -> ReasoningResult:
        refiner_executor = refiner_executor or executor
        bundles: list[TrajectoryBundle] = []
        total_s = total_f = 0

        for traj in self._trajectories(query, mode):
            bundle = await self._call(executor, traj)
            total_s += bundle.searches_used
            total_f += bundle.fetches_used

            for _ in range(self.budget.max_refinement_rounds - 1):
                gaps = find_evidence_gaps(bundle)
                if not gaps:
                    break
                repair_queries = build_repair_queries(gaps, traj.seed_query)[: self.budget.max_search_calls_per_trajectory]
                if not repair_queries:
                    break
                repair_traj = traj.model_copy(update={"queries": repair_queries, "round": bundle.rounds_used + 1})
                repaired = await self._call(refiner_executor, repair_traj)
                total_s += repaired.searches_used
                total_f += repaired.fetches_used
                bundle = _merge(bundle, repaired)

            bundles.append(bundle)

        ranked = self.reward.score_all(bundles)
        winner = ranked[0] if ranked else None
        within = total_s <= self.budget.search_ceiling and total_f <= self.budget.fetch_ceiling
        return ReasoningResult(
            query=query, mode=mode, winner=winner, bundles=ranked, explanation=_explain(ranked),
            total_searches=total_s, total_fetches=total_f, within_budget=within,
            disputed=bool(winner and winner.disputed_count > 0),
        )
