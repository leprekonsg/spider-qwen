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
# Above this total conflict, Dempster renormalization becomes misleading
# (Zadeh's paradox); switch to Yager and surface the conflict as unknown mass.
YAGER_CONFLICT_THRESHOLD = 0.8
# A fused interval whose Pl - Bel gap exceeds this tau has more than half its
# mass uncommitted: "we genuinely do not know". Such disputes must surface as
# proactive S3 risk signals instead of sitting silently in memory. Yager-rule
# fusions flag regardless of the gap -- the rule only engages above
# YAGER_CONFLICT_THRESHOLD pairwise conflict, which is itself the warning.
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
    conflict: float  # max pairwise conflict K observed during fusion
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


def _combine(m1: BeliefMass, m2: BeliefMass, *, yager: bool) -> tuple[BeliefMass, float]:
    """Combine two BPAs; returns (fused mass, pairwise conflict K)."""
    k = m1.true_mass * m2.false_mass + m1.false_mass * m2.true_mass
    t = m1.true_mass * m2.true_mass + m1.true_mass * m2.unknown_mass + m1.unknown_mass * m2.true_mass
    f = m1.false_mass * m2.false_mass + m1.false_mass * m2.unknown_mass + m1.unknown_mass * m2.false_mass
    u = m1.unknown_mass * m2.unknown_mass
    if yager:
        # Yager: conflict mass joins the unknown set instead of renormalizing.
        return BeliefMass(true_mass=t, false_mass=f, unknown_mass=u + k), k
    norm = 1.0 - k
    if norm <= 0.0:  # total conflict: Dempster undefined, all mass to unknown
        return BeliefMass(unknown_mass=1.0), k
    return BeliefMass(true_mass=t / norm, false_mass=f / norm, unknown_mass=u / norm), k


def fuse(masses: list[BeliefMass]) -> tuple[BeliefMass, float, str]:
    """Fuse BPAs sequentially; returns (mass, max pairwise K, rule used).

    Inputs are canonically sorted first: sequential combination under Yager is
    not associative and the max-pairwise-K measurement depends on accumulation
    order, so without sorting the same *multiset* of masses could pick a
    different rule or interval depending on input order. After sorting the
    result is a pure function of the multiset.

    A first Dempster pass measures conflict; if any pairwise K exceeds the
    threshold the fusion is redone under Yager so high conflict is *surfaced*
    as unknown mass rather than hidden by renormalization.
    """
    if not masses:
        return BeliefMass(), 0.0, "single"
    if len(masses) == 1:
        return masses[0], 0.0, "single"
    ordered = sorted(masses, key=lambda m: (m.true_mass, m.false_mass, m.unknown_mass))

    def _run(yager: bool) -> tuple[BeliefMass, float]:
        fused = ordered[0]
        max_k = 0.0
        for m in ordered[1:]:
            fused, k = _combine(fused, m, yager=yager)
            max_k = max(max_k, k)
        return fused, max_k

    fused, max_k = _run(yager=False)
    if max_k > YAGER_CONFLICT_THRESHOLD:
        fused, max_k = _run(yager=True)
        return fused, round(max_k, 6), "yager"
    return fused, round(max_k, 6), "dempster"


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
        if item is not None:
            out.append(float(item.reliability))
        else:
            out.append(_own_confidence(side))
    if not out:  # evidence-free side: weight by its recorded confidence alone
        out.append(_own_confidence(side))
    return out
