"""T-4.1: Reflections -- distil a handful of insights from recent memory.

A periodic job reads recent semantic facts + episodic records and writes 5-10
actionable insights (disputes to resolve, facts going stale, well-corroborated
facts, coverage, run outcomes). Deterministic by default; an optional
``llm`` seam (qwen3.5-flash in production) can add a free-text summary insight
but is OFF by default so the offline path stays network-free. Every insight that
derives from evidence-backed facts carries those evidence refs.
"""

from __future__ import annotations

from typing import Callable, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from .. import SCHEMA_VERSION
from ..evidence.models import EvidenceRef, utc_now_iso
from .decay import apply_decay, is_stale
from .episodic import EpisodicRecord
from .semantic import SemanticFact

ReflectionKind = Literal["dispute", "staleness", "corroboration", "coverage", "outcome", "summary"]


class Reflection(BaseModel):
    schema_version: str = SCHEMA_VERSION
    reflection_id: str = Field(default_factory=lambda: f"refl_{uuid4().hex[:12]}")
    kind: ReflectionKind
    insight: str
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now_iso)


def _refs(fact: SemanticFact) -> list[EvidenceRef]:
    refs = list(fact.evidence_refs)
    for alt in fact.disputed_alternatives:
        refs.extend(alt.evidence_refs)
    return refs


class ReflectionEngine:
    """Deterministic insight distiller with an optional LLM summary seam."""

    def reflect(
        self,
        facts: list[SemanticFact],
        episodes: list[EpisodicRecord] | None = None,
        *,
        max_insights: int = 10,
        llm: Callable[[str], str] | None = None,
    ) -> list[Reflection]:
        episodes = episodes or []
        out: list[Reflection] = []

        # 1. Disputes first -- they are the most actionable (block RFQ use).
        for fact in facts:
            if fact.status == "disputed":
                values = [fact.value] + [a.value for a in fact.disputed_alternatives]
                out.append(Reflection(
                    kind="dispute",
                    insight=f"Disputed: {fact.entity_name} {fact.field} has conflicting values "
                            f"({', '.join(v for v in values if v)}); revalidate before use.",
                    evidence_refs=_refs(fact),
                ))

        # 2. Facts going stale -- a single aggregate insight.
        stale = [f for f in facts if f.status == "stale" or is_stale(f)]
        if stale:
            names = ", ".join(sorted({f.entity_name for f in stale}))
            refs = [r for f in stale for r in _refs(f)]
            out.append(Reflection(
                kind="staleness",
                insight=f"{len(stale)} fact(s) are stale and need revalidation: {names}.",
                evidence_refs=refs,
            ))

        # 3. Best-corroborated fact -- worth caching/reusing.
        active = [f for f in facts if f.status == "active"]
        if active:
            top = max(active, key=lambda f: (f.reinforcement_count, apply_decay(f)))
            if top.reinforcement_count > 0:
                out.append(Reflection(
                    kind="corroboration",
                    insight=f"{top.entity_name} {top.field} corroborated across "
                            f"{top.reinforcement_count + 1} observations; safe to reuse.",
                    evidence_refs=_refs(top),
                ))

        # 4. Coverage of what we are tracking.
        if facts:
            counts: dict[str, int] = {}
            for f in facts:
                counts[f.entity_type] = counts.get(f.entity_type, 0) + 1
            summary = ", ".join(f"{n} {t}(s)" for t, n in sorted(counts.items()))
            out.append(Reflection(kind="coverage", insight=f"Tracking {summary}."))

        # 5. Run outcomes from episodic memory.
        if episodes:
            ok = sum(1 for e in episodes if e.outcome == "success")
            out.append(Reflection(
                kind="outcome",
                insight=f"{ok}/{len(episodes)} recent run(s) succeeded.",
                evidence_refs=[r for e in episodes for r in e.evidence_refs],
            ))

        # 6. Optional LLM free-text summary (seam; off by default).
        if llm is not None and facts:
            try:
                text = llm(self._summary_prompt(facts, episodes)).strip()
            except Exception:
                text = ""
            if text:
                out.append(Reflection(kind="summary", insight=text))

        return out[:max_insights]

    @staticmethod
    def _summary_prompt(facts: list[SemanticFact], episodes: list[EpisodicRecord]) -> str:
        lines = [f"- {f.entity_name} {f.field}={f.value} ({f.status})" for f in facts[:20]]
        return (
            "Summarise the most important procurement insight from these memory "
            "facts in one sentence (json not required):\n" + "\n".join(lines)
        )
