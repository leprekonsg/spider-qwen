"""Dempster-Shafer belief fusion for disputed facts.

Each side of a disputed (entity, property) contributes a Basic Probability
Assignment over the frame {true, false}: a source supporting the value assigns
mass ``r`` (its reliability prior) to {true}, a source backing a competing value
assigns ``r`` to {false}, and the remainder ``1 - r`` stays on the whole frame
(unknown). Sides combine with Dempster's rule; when total conflict is high the
fusion falls back to Yager's rule, which moves the conflict mass to unknown
instead of renormalizing it away.

The output is a calibrated ``[belief, plausibility]`` interval per competing
value instead of a single confidence number. The gap ``plausibility - belief``
is the epistemic uncertainty: wide gaps mean "we genuinely do not know", not
"the sources disagree mildly", and can gate proactive risk warnings.

Pure Python, deterministic, no dependencies.
"""

from __future__ import annotations

from pydantic import BaseModel

# Reliability is clamped below 1 so two flatly contradicting "certain" sources
# produce total-but-finite conflict (Dempster's rule is undefined at K = 1).
_MAX_RELIABILITY = 0.99
# Above this total conjunctive conflict q(empty), Dempster renormalization
# becomes misleading (Zadeh's paradox); switch to Yager and surface the
# conflict as unknown mass.
YAGER_CONFLICT_THRESHOLD = 0.8
# A fused interval whose Pl - Bel gap exceeds this tau has more than half its
# mass uncommitted: "we genuinely do not know". Such disputes must surface as
# proactive S3 risk signals instead of sitting silently in memory. Yager-rule
# fusions flag regardless of the gap -- the rule only engages above
# YAGER_CONFLICT_THRESHOLD total conflict, which is itself the warning.
UNCERTAINTY_TAU = 0.5


class BeliefMass(BaseModel):
    """BPA over the frame {true, false}: m(T), m(F), m({T,F})."""

    true_mass: float = 0.0
    false_mass: float = 0.0
    unknown_mass: float = 1.0


class BeliefInterval(BaseModel):
    """Fused [Bel, Pl] interval for one candidate value of a disputed fact."""

    value: str
    belief: float
    plausibility: float
    conflict: float  # total conjunctive conflict q(empty) across all sources
    rule: str  # dempster | yager | single
    supporting_sources: int = 0
    contradicting_sources: int = 0

    @property
    def uncertainty(self) -> float:
        """Epistemic gap Pl - Bel; wide gaps should gate risk warnings."""
        return round(self.plausibility - self.belief, 6)


def bpa(reliability: float, *, supports: bool) -> BeliefMass:
    """One source's BPA: reliability mass on its verdict, the rest unknown."""
    r = max(0.0, min(_MAX_RELIABILITY, float(reliability)))
    if supports:
        return BeliefMass(true_mass=r, unknown_mass=round(1.0 - r, 6))
    return BeliefMass(false_mass=r, unknown_mass=round(1.0 - r, 6))


def _conjunctive(masses: list[BeliefMass]) -> tuple[float, float, float, float]:
    """N-ary unnormalized conjunctive combination over the frame {true, false}.

    Closed form for the two-element frame: a joint assignment lands on {T}
    exactly when every source contributes {T} or unknown (minus the all-unknown
    case), symmetrically for {F}; the remainder is the total conflict q(empty).
    Products commute, so the result is a pure function of the multiset -- no
    canonicalization step needed.
    """
    p_true = p_false = p_unknown = 1.0
    for m in masses:
        p_true *= m.true_mass + m.unknown_mass
        p_false *= m.false_mass + m.unknown_mass
        p_unknown *= m.unknown_mass
    q_true = max(0.0, p_true - p_unknown)  # clamp float dust, never semantics
    q_false = max(0.0, p_false - p_unknown)
    conflict = max(0.0, 1.0 - q_true - q_false - p_unknown)
    return q_true, q_false, p_unknown, conflict


def fuse(masses: list[BeliefMass]) -> tuple[BeliefMass, float, str]:
    """Fuse all BPAs in one batch; returns (mass, total conflict, rule used).

    Dempster's rule is associative, but Yager's is only quasi-associative: the
    conflict mass must move to unknown ONCE, after combining every source. A
    sequential pairwise fold re-exposes the parked conflict to the next source
    (unknown intersect {T} = {T}), which can turn a flat contradiction into
    near-certainty. Both rules therefore share one n-ary conjunctive pass and
    differ only in where the final conflict mass goes: Dempster renormalizes
    it away (fine while conflict is low), Yager surfaces it as unknown.

    Inputs are validated loudly: a malformed BPA must fail here, not propagate
    a silently wrong [Bel, Pl] interval into RFQ drafts and risk signals.
    """
    for m in masses:
        floor = min(m.true_mass, m.false_mass, m.unknown_mass)
        total = m.true_mass + m.false_mass + m.unknown_mass
        if floor < 0.0 or abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"invalid BPA (true={m.true_mass}, false={m.false_mass}, "
                f"unknown={m.unknown_mass}): masses must be non-negative and sum to 1"
            )
    if not masses:
        return BeliefMass(), 0.0, "single"
    if len(masses) == 1:
        return masses[0], 0.0, "single"
    q_true, q_false, q_unknown, conflict = _conjunctive(masses)
    if conflict > YAGER_CONFLICT_THRESHOLD:
        fused = BeliefMass(true_mass=q_true, false_mass=q_false,
                           unknown_mass=q_unknown + conflict)
        return fused, round(conflict, 6), "yager"
    # conflict <= threshold < 1 here, so the Dempster denominator is positive.
    norm = 1.0 - conflict
    fused = BeliefMass(true_mass=q_true / norm, false_mass=q_false / norm,
                       unknown_mass=q_unknown / norm)
    return fused, round(conflict, 6), "dempster"


def _interval(value: str, fused: BeliefMass, conflict: float, rule: str,
              supporting: int, contradicting: int) -> BeliefInterval:
    return BeliefInterval(
        value=value,
        belief=round(fused.true_mass, 6),
        plausibility=round(fused.true_mass + fused.unknown_mass, 6),
        conflict=conflict, rule=rule,
        supporting_sources=supporting, contradicting_sources=contradicting,
    )


def fuse_disputed_fact(fact, ledger=None) -> list[BeliefInterval]:
    """[Bel, Pl] interval per competing value of a disputed SemanticFact.

    Source reliability comes from the run ledger when the evidence ref still
    resolves (``item.reliability``, the T-2.4 prior); otherwise the side's own
    recorded confidence stands in. Sides are duck-typed: the primary fact and
    each ``disputed_alternatives`` entry both expose value/confidence/
    evidence_refs.
    """
    sides = [fact] + list(getattr(fact, "disputed_alternatives", []) or [])
    if len(sides) == 1:
        reliabilities = _side_reliabilities(fact, ledger.get if ledger else None)
        fused, k, rule = fuse([bpa(r, supports=True) for r in reliabilities])
        return [_interval(fact.value, fused, k, rule, _distinct_sources(fact), 0)]

    intervals: list[BeliefInterval] = []
    for side in sides:
        masses: list[BeliefMass] = []
        supporting = contradicting = 0
        for other in sides:
            supports = other is side
            for r in _side_reliabilities(other, ledger.get if ledger else None):
                masses.append(bpa(r, supports=supports))
            count = _distinct_sources(other)
            if supports:
                supporting += count
            else:
                contradicting += count
        fused, k, rule = fuse(masses)
        intervals.append(_interval(side.value, fused, k, rule, supporting, contradicting))
    intervals.sort(key=lambda i: i.belief, reverse=True)
    return intervals


class _ClaimSide:
    """Duck-typed single side for fusing one undisputed claim's sources."""

    def __init__(self, value: str, confidence: float | None, evidence_refs: list) -> None:
        self.value = value
        self.confidence = confidence
        self.evidence_refs = evidence_refs


def quote_channel_interval(candidate, ledger=None) -> BeliefInterval | None:
    """[Bel, Pl] for a candidate's quote-channel claim, from its sources.

    Single-sided by construction: disputed facts never reach an RFQ draft
    (controller guardrail), so the interval expresses source commitment --
    Bel is the fused reliability mass, Pl - Bel the uncommitted remainder.
    """
    qc = getattr(candidate, "quote_channel", None)
    if qc is None or not getattr(qc, "value", ""):
        return None
    ref = getattr(qc, "evidence_ref", None)
    side = _ClaimSide(qc.value, getattr(qc, "confidence", None), [ref] if ref else [])
    return fuse_disputed_fact(side, ledger)[0]


def _own_confidence(side) -> float:
    """A side's recorded confidence; 0.5 only when truly absent (None counts
    as absent, an explicit 0.0 does not -- `or` would fabricate 0.5 for it)."""
    conf = getattr(side, "confidence", None)
    return 0.5 if conf is None else float(conf)


def _distinct_sources(side) -> int:
    """Independent sources backing a side: distinct ref URLs, not span count.

    Three spans cited from one page are one source; the interval's
    supporting/contradicting counts must not let a single page outvote
    a datasheet cited once.
    """
    refs = list(getattr(side, "evidence_refs", []) or [])
    if not refs:
        return 1  # the side itself (confidence stand-in) counts once
    return len({getattr(r, "url", "") or getattr(r, "ledger_id", "") for r in refs})


def _side_reliabilities(side, ledger_get) -> list[float]:
    """One reliability weight per evidence ref backing a side."""
    refs = list(getattr(side, "evidence_refs", []) or [])
    out: list[float] = []
    for ref in refs:
        item = ledger_get(ref.ledger_id) if ledger_get else None
        if item is None:
            out.append(_own_confidence(side))
        elif getattr(item, "source_tool", "") == "semantic_memory":
            out.extend(_memory_row_reliabilities(item))
        else:
            out.append(float(item.reliability))
    if not out:  # evidence-free side: weight by its recorded confidence alone
        out.append(_own_confidence(side))
    return out


def _memory_row_reliabilities(item) -> list[float]:
    """Weights for a synthetic semantic_memory recall row.

    The row's own T-2.4 tier reflects the vendor page the recall was attached
    to, not where the fact originally came from -- using it would miscalibrate
    the interval (same trap _source_class_for avoids in evidence/verifier.py).
    Weight by the ORIGINAL sources' tiers (metadata.source_evidence_refs,
    URL-classified; the pages live in a previous run's ledger), each capped by
    the row's recorded decayed recall confidence so a stale memory cannot claim
    its original pedigree at full strength. No resolvable originals -> the
    recall confidence alone.
    """
    from ..governance.source_reliability import (  # lazy: avoids import cycle
        DEFAULT_RELIABILITY,
        classify_source,
        host_of,
    )

    conf = getattr(item, "confidence", None)
    recall_conf = 0.5 if conf is None else float(conf)
    sources = (getattr(item, "metadata", {}) or {}).get("source_evidence_refs") or []
    tiers = [
        DEFAULT_RELIABILITY.get(classify_source(host_of(s.get("url", ""))),
                                DEFAULT_RELIABILITY["unknown"])
        for s in sources if isinstance(s, dict) and s.get("url")
    ]
    if not tiers:
        return [recall_conf]
    return [min(t, recall_conf) for t in tiers]
