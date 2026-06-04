"""T-1.1: serendipity output schema + deterministic scoring (scaffold).

Reshapes a ranked candidate list into the four-slot JSON shape the build plan
calls for:

    {primary_answer, s1_substitutes[], s2_long_tail_sources[],
     s3_risk_signals[], evidence_refs[], serendipity_score}

**Scaffold behaviour (Phase 1):** ``primary_answer`` is rank #1; ``s1_substitutes``
are ranks 2–4; ``s2_long_tail_sources`` are rank 5+. That satisfies the schema
acceptance test but is *not* true S1 substitute / S2 long-tail classification
yet — those arrive with the supplier graph (T-3.1), bandit/Wayback (T-5.x), and
upstream slot population. Do not describe this module as full serendipity discovery.

Each non-primary slot item is scored ``relevance x novelty x unexpectedness`` in
[0,1] against a top-ranked baseline (a proxy for the "top-3 authorized
distributor" baseline in the plan; source-type classification arrives in T-2.4).

Scoring is deterministic and LLM-free. Later phases inject richer slot content
via ``extra_risk_signals`` and by populating slots upstream before this reshape.
"""

from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from .. import SCHEMA_VERSION
from ..evidence.belief import UNCERTAINTY_TAU, fuse_disputed_fact
from ..evidence.models import EvidenceRef
from ..modes.contracts import PricingStatus

# Geo score saturates at EXACT_COUNTRY (20.0); a fully-local result is the least
# "unexpected", a globally-sourced one the most.
_GEO_SATURATION = 20.0
_BASELINE_SIZE = 3  # top-N ranked treated as the conventional baseline


class RiskSignal(BaseModel):
    """An S3 risk/serendipity signal (EOL/PCN, pricing conflict, counterfeit, ...)."""

    schema_version: str = SCHEMA_VERSION
    signal_type: str
    severity: str = "info"  # info | low | medium | high
    description: str = ""
    entity: str | None = None
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)


class SerendipitySlotItem(BaseModel):
    """A scored non-primary slot entry (substitute or long-tail source)."""

    schema_version: str = SCHEMA_VERSION
    candidate: dict[str, Any]
    serendipity_score: float = 0.0
    relevance: float = 0.0
    novelty: float = 0.0
    unexpectedness: float = 0.0
    rationale: str = ""


class SerendipityResult(BaseModel):
    schema_version: str = SCHEMA_VERSION
    primary_answer: dict[str, Any] | None = None
    s1_substitutes: list[SerendipitySlotItem] = Field(default_factory=list)
    s2_long_tail_sources: list[SerendipitySlotItem] = Field(default_factory=list)
    s3_risk_signals: list[RiskSignal] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    serendipity_score: float = 0.0


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def _registrable(url: str | None) -> str:
    if not url:
        return ""
    host = urlparse(url).netloc.lower() or (url or "").lower()
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _identity(candidate) -> str:
    return f"{getattr(candidate, 'vendor_name', '')} {getattr(candidate, 'website', '') or ''}".strip().lower()


def _relevance(candidate, max_score: float) -> float:
    return _clamp(float(getattr(candidate, "score", 0.0)) / max_score) if max_score > 0 else 0.0


def _novelty(candidate, baseline: list) -> float:
    """1 - closest textual similarity to a baseline entry (self excluded)."""
    me = _identity(candidate)
    my_domain = _registrable(getattr(candidate, "website", None))
    best = 0.0
    for other in baseline:
        if other is candidate:
            continue
        sim = SequenceMatcher(None, me, _identity(other)).ratio()
        if my_domain and my_domain == _registrable(getattr(other, "website", None)):
            sim = max(sim, 0.7)  # same registrable domain is not novel
        best = max(best, sim)
    return _clamp(1.0 - best)


def _unexpectedness(candidate) -> float:
    """Local, mainstream sources are expected; off-region/long-tail are not."""
    geo = float(getattr(candidate, "geo_score", 0.0))
    return _clamp(1.0 - geo / _GEO_SATURATION)


def _slot_item(candidate, max_score: float, baseline: list) -> SerendipitySlotItem:
    rel = round(_relevance(candidate, max_score), 4)
    nov = round(_novelty(candidate, baseline), 4)
    unexp = round(_unexpectedness(candidate), 4)
    score = round(_clamp(rel * nov * unexp), 4)
    return SerendipitySlotItem(
        candidate=candidate.model_dump(mode="json"),
        serendipity_score=score,
        relevance=rel,
        novelty=nov,
        unexpectedness=unexp,
        rationale=f"relevance={rel} x novelty={nov} x unexpectedness={unexp}",
    )


def _risk_signals_from_candidates(candidates: list) -> list[RiskSignal]:
    signals: list[RiskSignal] = []
    for c in candidates:
        status = getattr(c, "pricing_status", None)
        if status == PricingStatus.CONFLICTING:
            signals.append(RiskSignal(
                signal_type="pricing_conflict",
                severity="high",
                description=f"Conflicting prices observed for {c.vendor_name}",
                entity=c.vendor_name,
                evidence_refs=list(getattr(c, "evidence_refs", [])),
            ))
    return signals


def disputed_fact_signals(facts, ledger=None, *, tau: float | None = None) -> list[RiskSignal]:
    """S3 RiskSignals for disputed facts whose fused [Bel, Pl] gap is wide.

    Each disputed fact is fused with Dempster-Shafer (evidence/belief.py); a
    signal is emitted when the best value's uncertainty (Pl - Bel) reaches
    ``tau`` (default UNCERTAINTY_TAU) or the fusion fell back to Yager's rule
    -- the rule only engages above YAGER_CONFLICT_THRESHOLD conflict, which is
    itself the warning. Signals carry every side's evidence refs.
    """
    threshold = UNCERTAINTY_TAU if tau is None else tau
    signals: list[RiskSignal] = []
    for fact in facts:
        if getattr(fact, "status", "") != "disputed":
            continue
        intervals = fuse_disputed_fact(fact, ledger)
        top = intervals[0]
        if top.uncertainty < threshold and top.rule != "yager":
            continue
        competing = ", ".join(i.value for i in intervals[1:]) or "none"
        refs = list(getattr(fact, "evidence_refs", []) or [])
        for alt in getattr(fact, "disputed_alternatives", []) or []:
            refs.extend(getattr(alt, "evidence_refs", []) or [])
        signals.append(RiskSignal(
            signal_type="belief_uncertainty",
            severity="high" if top.rule == "yager" else "medium",
            description=(
                f"Disputed {fact.field} for {fact.entity_name}: best value "
                f"'{top.value}' has [Bel, Pl] = [{top.belief}, {top.plausibility}] "
                f"(uncertainty {top.uncertainty}, rule {top.rule}); competing: {competing}"
            ),
            entity=fact.entity_name,
            evidence_refs=_dedupe_refs(refs),
        ))
    return signals


def build_serendipity_result(
    ranked: list,
    mode: str,
    *,
    extra_risk_signals: list[RiskSignal] | None = None,
) -> SerendipityResult:
    """Build the four-slot serendipity result from a ranked candidate list.

    ``ranked`` must already be sorted best-first (the per-mode ranker output).
    ``extra_risk_signals`` lets later phases inject S3 signals (EOL/PCN,
    counterfeit, OOD price) into the same slot.
    """
    if not ranked:
        signals = list(extra_risk_signals or [])
        return SerendipityResult(
            s3_risk_signals=signals,
            evidence_refs=_dedupe_refs(r for s in signals for r in s.evidence_refs),
        )

    # Defensive re-sort: the per-mode ranker already orders best-first, but make
    # primary == highest-scored regardless of caller ordering.
    ranked = sorted(ranked, key=lambda c: float(getattr(c, "score", 0.0)), reverse=True)
    max_score = max((float(getattr(c, "score", 0.0)) for c in ranked), default=0.0)
    baseline = ranked[:_BASELINE_SIZE]

    primary = ranked[0]
    s1 = [_slot_item(c, max_score, baseline) for c in ranked[1:4]]
    s2 = [_slot_item(c, max_score, baseline) for c in ranked[4:]]

    signals = _risk_signals_from_candidates(ranked)
    signals.extend(extra_risk_signals or [])

    refs: list[EvidenceRef] = []
    refs.extend(getattr(primary, "evidence_refs", []))
    for item in s1 + s2:
        for raw in item.candidate.get("evidence_refs", []):
            refs.append(EvidenceRef.model_validate(raw))
    for sig in signals:
        refs.extend(sig.evidence_refs)

    non_primary = s1 + s2
    overall = (
        round(sum(i.serendipity_score for i in non_primary) / len(non_primary), 4)
        if non_primary else 0.0
    )

    return SerendipityResult(
        primary_answer=primary.model_dump(mode="json"),
        s1_substitutes=s1,
        s2_long_tail_sources=s2,
        s3_risk_signals=signals,
        evidence_refs=_dedupe_refs(refs),
        serendipity_score=overall,
    )


def _dedupe_refs(refs) -> list[EvidenceRef]:
    seen: set[str] = set()
    out: list[EvidenceRef] = []
    for ref in refs:
        if ref is None or ref.ledger_id in seen:
            continue
        seen.add(ref.ledger_id)
        out.append(ref)
    return out
