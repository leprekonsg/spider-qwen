"""Deterministic planner.

Qwen acts as planner/controller in spirit, but v1 execution is deterministic and
policy-bound: the plan is a fixed pipeline per mode (search -> fetch -> extract
-> rank -> [rfq] -> persist). An LLM planner can be injected later behind the
same interface without changing the controller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..modes.router import RoutePlan
from ..serendipity.query_rewrite import SearchQuery, expand_query


@dataclass(frozen=True)
class PlanStep:
    name: str
    description: str


class Planner:
    def plan(self, route: RoutePlan) -> list[PlanStep]:
        steps = [
            PlanStep("search", "SEA/geo templates + Step-Back/HyDE expansion (SEA-first, global fallback)"),
            PlanStep("fetch", "Retrieve clean page content for candidate URLs"),
            PlanStep("extract", f"Run extractors: {', '.join(route.extractors)}"),
            PlanStep("rank", f"Score with the {route.ranker} ranker"),
        ]
        if route.produces_rfq:
            steps.append(PlanStep("rfq", "Generate RFQ drafts (draft only, never sent)"))
        steps.append(PlanStep("persist", "Write evidence ledger, episodic memory, traces"))
        return steps

    def expand_query(
        self,
        query: str,
        *,
        mode: str | None = None,
        llm: Callable[[str], str] | None = None,
    ) -> list[SearchQuery]:
        """Step-Back + HyDE + Query2Doc expansion for vague/obsolete queries (T-1.2)."""
        return expand_query(query, mode=mode, llm=llm)
