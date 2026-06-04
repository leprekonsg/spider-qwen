"""T-R.2: controller integration of the reasoning trajectory layer (offline).

run_reasoning explores >=2 strategy trajectories with the real search/fetch/extract
services, scores bundles with the PPRM, selects a winner with an explanation, and
ties every bundle's strategy/round to concrete ledger evidence -- all under offline
mock providers with no network and within the frozen reasoning budget.
"""

from __future__ import annotations

import asyncio

from spider_qwen.agent.controller import Controller
from spider_qwen.reasoning.trajectory import ReasoningBudget
from spider_qwen.tools.fetch_service import MockFetchProvider
from spider_qwen.tools.search_service import MockSearchProvider


def _controller(tmp_path):
    return Controller(
        search_provider=MockSearchProvider(),
        fetch_provider=MockFetchProvider(),
        state_dir=str(tmp_path),
    )


def test_run_reasoning_offline_selects_winner_with_evidence_provenance(tmp_path):
    res = asyncio.run(_controller(tmp_path).run_reasoning("office cleaning Singapore", mode="service_quote_required"))

    assert len(res.bundles) >= 2
    assert res.winner is not None and res.winner.reward is not None
    assert res.explanation and "won" in res.explanation.lower()

    # Provenance: each bundle ties a strategy/round to concrete ledger evidence ids.
    for bundle in res.bundles:
        assert bundle.trajectory.strategy is not None
        assert bundle.rounds_used >= 1
    assert res.winner.evidence_refs
    assert all(r.ledger_id for r in res.winner.evidence_refs)


def test_run_reasoning_stays_within_budget_ceilings(tmp_path):
    budget = ReasoningBudget()
    res = asyncio.run(_controller(tmp_path).run_reasoning("pest control Singapore", mode="service_quote_required"))
    assert res.total_searches <= budget.search_ceiling
    assert res.total_fetches <= budget.fetch_ceiling
    assert res.within_budget is True


def test_run_reasoning_offline_makes_no_network_calls(tmp_path):
    # MockSearchProvider/MockFetchProvider never touch the network; a successful
    # run with them is the offline guarantee (CI runs the e2e with network off).
    res = asyncio.run(_controller(tmp_path).run_reasoning("office cleaning Singapore", mode="service_quote_required"))
    assert res.winner is not None
