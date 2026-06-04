"""Deterministic planner.

Qwen acts as planner/controller in spirit, but v1 execution is deterministic and
policy-bound: the plan is a fixed pipeline per mode (search -> fetch -> extract
-> rank -> [rfq] -> persist). An LLM planner can be injected later behind the
same interface without changing the controller.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Callable

from ..modes.router import RoutePlan
from ..serendipity.query_rewrite import SearchQuery, expand_query

if TYPE_CHECKING:
    from ..skills.models import AgentSkill
    from ..skills.registry import SkillRegistry


@lru_cache(maxsize=1)
def _default_skill_registry() -> "SkillRegistry":
    from ..skills.registry import SkillRegistry

    return SkillRegistry.load()


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

    def select_skills(
        self,
        query: str,
        *,
        top_k: int = 3,
        registry: "SkillRegistry | None" = None,
    ) -> list[AgentSkill]:
        """Auto-select project Qwen Agent Skills relevant to the query (T-7.2).

        Deterministic description match; the planner picks which skills' domain
        instructions a Qwen-assisted step would consult. Returns [] when no
        skill's description overlaps the query.
        """
        reg = registry if registry is not None else _default_skill_registry()
        return [m.skill for m in reg.match(query, top_k=top_k)]
