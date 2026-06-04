"""T-R.2: recursive repair -- round-1 explore, round-2 repair only weak areas.

After a trajectory's first pass, detect evidence gaps (missing quote channel,
incomplete checklist, unconfirmed geography, FFF/datasheet conflict, cross-source
price contradiction) and emit targeted repair queries. Deterministic; the runner
caps how many repair rounds run so the loop can never run away.
"""

from __future__ import annotations

from dataclasses import dataclass

from .trajectory import TrajectoryBundle

_SERVICE_MODES = {"service_quote_required", "contact_enrichment_only"}


@dataclass(frozen=True)
class EvidenceGap:
    kind: str
    detail: str


def find_evidence_gaps(bundle: TrajectoryBundle) -> list[EvidenceGap]:
    gaps: list[EvidenceGap] = []
    m = bundle.metrics
    mode = bundle.trajectory.mode
    if mode in _SERVICE_MODES:
        if m.quote_channel < 0.5:
            gaps.append(EvidenceGap("quote_channel", "no validated quote channel"))
        if m.checklist < 0.6:
            gaps.append(EvidenceGap("checklist", "required-inputs checklist incomplete"))
        if m.geo < 0.5:
            gaps.append(EvidenceGap("geo", "vendor geography unconfirmed"))
    else:
        if m.fff_similarity < 0.5:
            gaps.append(EvidenceGap("fff_conflict", "FFF equivalence unconfirmed"))
        if m.datasheet_evidence < 0.5:
            gaps.append(EvidenceGap("datasheet", "datasheet evidence missing"))
    if bundle.disputed_count > 0:
        gaps.append(EvidenceGap("price_conflict", "cross-source contradiction"))
    return gaps


_REPAIR_TEMPLATES = {
    "quote_channel": "{q} request a quote contact sales RFQ",
    "checklist": "{q} service scope pricing requirements FAQ",
    "geo": "{q} location address office",
    "fff_conflict": "{q} datasheet pinout package equivalent",
    "datasheet": "{q} datasheet archive specification",
    "price_conflict": "{q} official latest price datasheet",
}


def build_repair_queries(gaps: list[EvidenceGap], seed_query: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for gap in gaps:
        template = _REPAIR_TEMPLATES.get(gap.kind)
        if not template:
            continue
        query = template.format(q=seed_query)
        if query not in seen:
            seen.add(query)
            out.append(query)
    return out
