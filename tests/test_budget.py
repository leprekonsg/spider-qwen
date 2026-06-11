from __future__ import annotations

import pytest

from spider_qwen.agent.budget import Budget, BudgetExceeded, BudgetTracker, StopReason
from spider_qwen.agent.tool_registry import ToolRegistry


def test_search_budget_blocks_after_limit():
    tracker = BudgetTracker(Budget(mode="service_quote_required", max_search_calls=2))
    tracker.consume_search()
    tracker.consume_search()
    with pytest.raises(BudgetExceeded) as exc:
        tracker.consume_search()
    assert exc.value.reason == StopReason.MAX_SEARCH_CALLS_REACHED


def test_fetch_budget_clamps_to_remaining():
    tracker = BudgetTracker(Budget(mode="service_quote_required", max_fetch_urls=5))
    allowed = tracker.consume_fetch(10)
    assert allowed == 5
    with pytest.raises(BudgetExceeded) as exc:
        tracker.consume_fetch(1)
    assert exc.value.reason == StopReason.MAX_FETCH_URLS_REACHED


def test_extraction_budget():
    tracker = BudgetTracker(Budget(mode="x", max_candidates_to_extract=1))
    assert tracker.consume_extraction() is True
    assert tracker.consume_extraction() is False


def test_runtime_budget_zero_blocks():
    tracker = BudgetTracker(Budget(mode="x", max_runtime_seconds=0))
    assert tracker.runtime_exceeded() is True
    with pytest.raises(BudgetExceeded):
        tracker.consume_search()


def test_tool_registry_runtime_guard_rejects_forbidden_tool():
    with pytest.raises(ValueError, match="not allowed"):
        ToolRegistry.require_allowed("browser_drive")
