"""T-5.4 (core): proactive S3 risk signals.

Two leading-indicator capabilities, deterministic and offline:

(a) ``diff_datasheet(old, new)`` -- a lifecycle worsening (active -> NRND -> EOL),
    a newly-appearing PCN/PDN, or a "second source removed" note between two
    snapshots is a 3-9-month leading risk indicator -> a ``RiskSignal``.
(d) ``proactive_watch(mpn, text, store)`` -- when a part reads NRND/EOL (or carries
    a PCN), auto-surface a deprecation signal plus substitute alternates pulled from
    the supplier-part graph (the CROSS_REFERENCE / successor edges mined by T-3.1 /
    T-5.1), without being asked. Alternates come only from the graph -- never
    fabricated -- and each carries the asserting claim's ledger_id.

Out of scope (demoted to NICE in the v2 amendment): the OOD price/lead-time monitor
and the counterfactual-BOM "what-if" recompute. Standalone + opt-in: the controller
mid-quote interjection is the Phase 8 integration (deviation #4 pattern).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from .. import SCHEMA_VERSION
from ..evidence.models import EvidenceRef
from ..graph.schema import part_key
from ..ranking.serendipity import RiskSignal

# Severity-ranked lifecycle states (higher = worse). "active"/"unknown" are benign.
LIFECYCLE_SEVERITY = {
    "unknown": 0, "active": 0, "preliminary": 1, "nrnd": 2, "ltb": 2, "eol": 3,
}
# Marker phrases per state, checked most-severe first so the worst present wins.
# Matching is word-boundary anchored (see _compile) so a token never matches inside
# a part number or another word ("nrnd" in MX25L1606ENRND, "active" in "inactive").
_LIFECYCLE_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("eol", ("end of life", "end-of-life", "obsolete", "discontinued", "eol")),
    ("ltb", ("last time buy", "last-time-buy", "ltb")),
    ("nrnd", ("not recommended for new design", "nrnd")),
    ("preliminary", ("preliminary", "advance information")),
    ("active", ("active", "in production", "recommended for new design")),
)


def _compile(markers: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(re.escape(m) for m in markers) + r")\b")


_LIFECYCLE_PATTERNS = tuple((state, _compile(markers)) for state, markers in _LIFECYCLE_MARKERS)
# Full PCN/PDN phrases are unambiguous; the bare acronym only counts when it carries
# a notification id (PCN-12345), so "EPCN bus" / "PDN design" do not false-trigger.
_PCN_PHRASE = re.compile(r"\b(?:product change notification|product discontinuation(?: notice)?)\b")
_PCN_ID = re.compile(r"\b(?:pcn|pdn)\b[\s#:.-]*\d")
_SECOND_SOURCE_PATTERN = _compile(("second source removed", "second-source removed"))

# Graph relations that point at a usable substitute / successor part.
SUBSTITUTE_RELS = (
    "CROSS_REFERENCE", "SUPERSEDED_BY", "PIN_COMPATIBLE_WITH", "SAME_DIE_AS", "RENAMED_TO",
)

_SEVERITY_LABEL = {"eol": "high", "ltb": "medium", "nrnd": "medium", "preliminary": "low"}


class Alternate(BaseModel):
    schema_version: str = SCHEMA_VERSION
    mpn: str
    part_id: str
    relation: str
    confidence: float = 0.0
    reliability: float = 0.0
    evidence_claim_id: str


class ProactiveWatchResult(BaseModel):
    schema_version: str = SCHEMA_VERSION
    mpn: str
    part_id: str
    lifecycle_status: str = "unknown"
    triggered: bool = False
    risk_signal: RiskSignal | None = None
    alternates: list[Alternate] = Field(default_factory=list)


def detect_lifecycle(text: str) -> str:
    """Most severe lifecycle state named in ``text`` (``unknown`` if none)."""
    low = (text or "").lower()
    for state, pattern in _LIFECYCLE_PATTERNS:
        if pattern.search(low):
            return state
    return "unknown"


def _has_pcn(text: str) -> bool:
    low = (text or "").lower()
    return bool(_PCN_PHRASE.search(low) or _PCN_ID.search(low))


def _has_second_source_removed(text: str) -> bool:
    return bool(_SECOND_SOURCE_PATTERN.search((text or "").lower()))


def diff_datasheet(
    old_text: str,
    new_text: str,
    *,
    mpn: str | None = None,
    evidence_refs: list[EvidenceRef] | None = None,
) -> RiskSignal | None:
    """Emit a deprecation RiskSignal when ``new_text`` is worse than ``old_text``.

    Triggers: lifecycle severity increased, a PCN/PDN newly appeared, or a
    "second source removed" note newly appeared. No regression -> ``None``.
    """
    old_state, new_state = detect_lifecycle(old_text), detect_lifecycle(new_text)
    worsened = LIFECYCLE_SEVERITY.get(new_state, 0) > LIFECYCLE_SEVERITY.get(old_state, 0)
    new_pcn = _has_pcn(new_text) and not _has_pcn(old_text)
    lost_source = _has_second_source_removed(new_text) and not _has_second_source_removed(old_text)
    if not (worsened or new_pcn or lost_source):
        return None

    reasons = []
    if worsened:
        reasons.append(f"lifecycle {old_state} -> {new_state}")
    if new_pcn:
        reasons.append("new PCN/PDN")
    if lost_source:
        reasons.append("second source removed")
    severity = _SEVERITY_LABEL.get(new_state, "medium" if (new_pcn or lost_source) else "low")
    return RiskSignal(
        signal_type="lifecycle_deprecation",
        severity=severity,
        description=f"Datasheet diff: {', '.join(reasons)}.",
        entity=mpn,
        evidence_refs=list(evidence_refs or []),
    )


def proactive_watch(
    mpn: str,
    text: str,
    store,
    *,
    evidence_refs: list[EvidenceRef] | None = None,
    rels: tuple[str, ...] = SUBSTITUTE_RELS,
) -> ProactiveWatchResult:
    """Surface a deprecation signal + graph-sourced alternates for an at-risk part.

    "At risk" = lifecycle severity >= NRND, or a PCN present. Alternates are the
    part's substitute/successor neighbours in the graph (evidence-backed), not
    fabricated; if the graph holds fewer than wanted, fewer are returned.
    """
    pid = part_key(mpn)
    status = detect_lifecycle(text)
    pcn = _has_pcn(text)
    at_risk = LIFECYCLE_SEVERITY.get(status, 0) >= LIFECYCLE_SEVERITY["nrnd"] or pcn
    result = ProactiveWatchResult(mpn=mpn, part_id=pid, lifecycle_status=status)
    if not at_risk:
        return result

    alternates: list[Alternate] = []
    seen: set[str] = set()
    for edge in store.neighbors(pid, rels=rels):
        dst = edge["dst"]
        if dst == pid or dst in seen:
            continue
        seen.add(dst)
        node = store.get_node(dst)
        surface = (node or {}).get("props", {}).get("surface", dst)
        alternates.append(Alternate(
            mpn=surface, part_id=dst, relation=edge["rel"],
            confidence=edge["confidence"], reliability=edge["reliability"],
            evidence_claim_id=edge["evidence_claim_id"],
        ))

    label = "pcn_alert" if pcn and status not in {"nrnd", "eol", "ltb"} else "lifecycle_deprecation"
    reason = "PCN/PDN issued" if label == "pcn_alert" else f"lifecycle status {status}"
    result.triggered = True
    result.risk_signal = RiskSignal(
        signal_type=label,
        severity=_SEVERITY_LABEL.get(status, "medium"),
        description=f"Proactive watch: {mpn} flagged ({reason}); {len(alternates)} alternate(s) surfaced.",
        entity=mpn,
        evidence_refs=list(evidence_refs or []),
    )
    result.alternates = alternates
    return result
