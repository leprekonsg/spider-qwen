"""T-6.1: DMSMS (SD-22) obsolescence mitigation.

Three deterministic, procurement-grounded outputs for an obsolete part:

- ``build_dmsms_ladder`` -- the SD-22 six-strategy resolution ladder in priority
  order (existing stock -> reclamation -> substitute -> alternate source ->
  redesign -> emulation). An EOL detection returns the whole structured ladder,
  not a single quote; each rung is flagged applicable based on the supplied
  context (on-hand stock, graph substitutes, aftermarket sources).
- ``ltb_quantity`` -- a Last-Time-Buy bridge-buy size = expected demand over the
  remaining service life + a cost-sensitive safety stock (newsvendor critical
  ratio over shortage vs holding+disposal cost).
- ``counterfeit_badge`` -- cross-checks a source against ERAI bad-supplier and
  GIDEP advisory fixtures and the source's authorization tier, rendering a
  red/amber/green badge with AS6081/AS6171 status and the FAR 52.246-26 citation.

Standalone + opt-in: embedding the ladder/badge into an electronics RFQ draft is
the Phase 8 integration (deviation #4); the service RFQ generator is unchanged.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, Field

from .. import SCHEMA_VERSION
from ..evidence.models import EvidenceRef

DmsmsStrategy = Literal[
    "existing_stock", "reclamation", "substitute", "alternate_source", "redesign", "emulation",
]
# SD-22 mitigation hierarchy, cheapest/least-disruptive first.
DMSMS_LADDER_ORDER: tuple[DmsmsStrategy, ...] = (
    "existing_stock", "reclamation", "substitute", "alternate_source", "redesign", "emulation",
)
_RUNG_TITLE: dict[DmsmsStrategy, str] = {
    "existing_stock": "Use existing stock / last-time buy",
    "reclamation": "Reclamation or refurbishment",
    "substitute": "Form-fit-function substitute",
    "alternate_source": "Aftermarket / alternate source",
    "redesign": "Redesign (minor or major)",
    "emulation": "Emulation / aftermarket fabrication",
}
_RUNG_DETAIL: dict[DmsmsStrategy, str] = {
    "existing_stock": "Consume on-hand inventory or place a last-time buy to bridge remaining demand.",
    "reclamation": "Recover the part from retired/scrapped assemblies and re-qualify it.",
    "substitute": "Qualify a form-fit-function equivalent or successor part.",
    "alternate_source": "Source from an authorized aftermarket (e.g. Rochester/Lansdale) or a screened broker.",
    "redesign": "Redesign the assembly to remove or replace the obsolete part (higher cost/NRE).",
    "emulation": "Last resort: aftermarket FFF emulation / re-fabrication of the original die.",
}
# Rungs that are always technically available as a fallback, regardless of context.
_ALWAYS_APPLICABLE: frozenset[DmsmsStrategy] = frozenset({"redesign", "emulation"})

RiskLevel = Literal["red", "amber", "green"]
# Source tiers that are franchised/authorized -> low counterfeit risk.
_AUTHORIZED_SOURCES = frozenset({"manufacturer", "authorised_distributor", "authorized_distributor", "franchised"})


class DmsmsRung(BaseModel):
    schema_version: str = SCHEMA_VERSION
    strategy: DmsmsStrategy
    rank: int
    title: str
    applicable: bool = False
    detail: str = ""
    candidates: list[str] = Field(default_factory=list)


class LtbRecommendation(BaseModel):
    schema_version: str = SCHEMA_VERSION
    annual_demand: float
    service_life_years: float
    safety_stock: int = 0
    quantity: int = 0
    rationale: str = ""


class CounterfeitBadge(BaseModel):
    schema_version: str = SCHEMA_VERSION
    source: str
    risk_level: RiskLevel
    erai_flagged: bool = False
    gidep_flagged: bool = False
    as6081_status: str = ""
    as6171_status: str = ""
    far_citation: str = "FAR 52.246-26"
    rationale: str = ""


class DmsmsLadder(BaseModel):
    schema_version: str = SCHEMA_VERSION
    mpn: str
    lifecycle_status: str = "unknown"
    rungs: list[DmsmsRung] = Field(default_factory=list)
    ltb: LtbRecommendation | None = None
    counterfeit_badges: list[CounterfeitBadge] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)


def ltb_quantity(
    annual_demand: float,
    service_life_years: float,
    *,
    holding_cost: float = 0.0,
    shortage_cost: float = 0.0,
    disposal_cost: float = 0.0,
    safety_factor: float = 0.15,
) -> LtbRecommendation:
    """Last-Time-Buy bridge quantity: demand over service life + cost-aware safety stock."""
    demand = max(0.0, annual_demand)
    years = max(0.0, service_life_years)
    base = demand * years
    carry = holding_cost + disposal_cost
    # Newsvendor critical ratio: shortage cost vs total carrying cost. >0.5 -> hold
    # more (stockout is worse than carrying); <0.5 -> hold less.
    if shortage_cost > 0 and (shortage_cost + carry) > 0:
        critical_ratio = shortage_cost / (shortage_cost + carry)
    else:
        critical_ratio = 0.5
    safety_stock = math.ceil(demand * safety_factor * (0.5 + critical_ratio))
    quantity = math.ceil(base) + safety_stock
    rationale = (
        f"Bridge buy = demand {demand:g}/yr x {years:g}-yr service life ({math.ceil(base)}) "
        f"+ safety stock {safety_stock} (critical ratio {critical_ratio:.2f})."
    )
    return LtbRecommendation(
        annual_demand=demand, service_life_years=years,
        safety_stock=safety_stock, quantity=quantity, rationale=rationale,
    )


def build_dmsms_ladder(
    mpn: str,
    *,
    lifecycle_status: str = "eol",
    on_hand: int = 0,
    alternates: list[str] | None = None,
    alt_sources: list[str] | None = None,
    reclamation_available: bool = False,
    ltb: LtbRecommendation | None = None,
    evidence_refs: list[EvidenceRef] | None = None,
) -> DmsmsLadder:
    """Build the SD-22 six-rung mitigation ladder for an obsolete part.

    The ladder always has all six rungs (a structured resolution, not a single
    quote); each rung's ``applicable`` flag reflects the supplied context.
    """
    alternates = alternates or []
    alt_sources = alt_sources or []
    applicable = {
        "existing_stock": on_hand > 0 or ltb is not None,
        "reclamation": reclamation_available,
        "substitute": bool(alternates),
        "alternate_source": bool(alt_sources),
        "redesign": True,
        "emulation": True,
    }
    candidates = {
        "existing_stock": ([f"{on_hand} on hand"] if on_hand > 0 else [])
        + ([f"LTB {ltb.quantity}"] if ltb is not None else []),
        "substitute": list(alternates),
        "alternate_source": list(alt_sources),
    }
    rungs = [
        DmsmsRung(
            strategy=strat, rank=i + 1, title=_RUNG_TITLE[strat],
            applicable=strat in _ALWAYS_APPLICABLE or applicable.get(strat, False),
            detail=_RUNG_DETAIL[strat], candidates=candidates.get(strat, []),
        )
        for i, strat in enumerate(DMSMS_LADDER_ORDER)
    ]
    return DmsmsLadder(
        mpn=mpn, lifecycle_status=lifecycle_status, rungs=rungs, ltb=ltb,
        evidence_refs=list(evidence_refs or []),
    )


def _norm(name: str) -> str:
    return " ".join((name or "").lower().split())


def counterfeit_badge(
    source: str,
    *,
    source_type: str = "unknown",
    erai_list: list[str] | tuple[str, ...] = (),
    gidep_list: list[str] | tuple[str, ...] = (),
    as_screened: bool = False,
) -> CounterfeitBadge:
    """Render a counterfeit-risk badge for a sourcing option.

    red   = named on the ERAI bad-supplier list or a GIDEP advisory.
    green = a franchised/authorized source (manufacturer or authorized distributor).
    amber = an unauthorized source (broker/marketplace) not on a bad list: AS6081/
            AS6171 incoming inspection is required before use.
    """
    key = _norm(source)
    erai = key in {_norm(s) for s in erai_list}
    gidep = key in {_norm(s) for s in gidep_list}
    authorized = source_type in _AUTHORIZED_SOURCES

    if erai or gidep:
        level: RiskLevel = "red"
    elif authorized:
        level = "green"
    else:
        level = "amber"

    if authorized:
        as6081 = as6171 = "n/a (franchised source)"
    elif as_screened:
        as6081, as6171 = "screened (AS6081)", "screened (AS6171)"
    else:
        as6081, as6171 = "required (AS6081 incoming inspection)", "required (AS6171 test)"

    reasons = []
    if erai:
        reasons.append("listed on ERAI bad-supplier data")
    if gidep:
        reasons.append("named in a GIDEP advisory")
    if not (erai or gidep):
        reasons.append("authorized franchised source" if authorized else "unauthorized source; counterfeit screening required")
    return CounterfeitBadge(
        source=source, risk_level=level, erai_flagged=erai, gidep_flagged=gidep,
        as6081_status=as6081, as6171_status=as6171,
        rationale="; ".join(reasons) + ".",
    )
