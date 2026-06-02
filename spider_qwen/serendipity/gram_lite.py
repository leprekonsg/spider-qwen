"""T-3.3: GRAM-lite "Serendipity Mode" (width-first, depth-capped, budgeted).

Width-first: sample ``S=5`` diverse query trajectories (synonym / broader-class /
broker-operator / SEA-local-language / mfr-alt-PN) -- reusing the T-1.2 expansion
for diversity -- run them, and recurse to ``D_MAX=3`` with per-hop confidence
``x0.85``. A verification fan-out of ``K=3`` lenses (re-fetch / competing-vendor /
Wayback) routes disagreements to the disputed handler (T-2.3). Every recursion
edge is recorded as ``(parent_sha, child_sha, depth, query, ts)``.

Deterministic and offline by default: ``fetch_fn`` / ``verify_fn`` / ``llm`` /
``disputed_handler`` are injectable seams. Hard caps (<=1 Max + <=25 flash + <=45
fetch per top-level query) are enforced by construction -- the recursion stops
before a cap is crossed, so ``within_caps`` is always true.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from pydantic import BaseModel, Field

from .query_rewrite import expand_query

WIDTH_S = 5
DEPTH_MAX = 3
HOP_DECAY = 0.85
VERIFY_K = 3
VERIFY_LENSES = ("refetch", "competing_vendor", "wayback")

CATEGORIES = ("synonym", "broader_class", "broker_operator", "sea_local_language", "mfr_alt_pn")
_KIND_TO_CATEGORY = {
    "query2doc": "synonym",
    "hyde": "synonym",
    "step_back": "broader_class",
    "broker_operator": "broker_operator",
    "mpn_pattern": "mfr_alt_pn",
    "obsolescence": "mfr_alt_pn",
}
_SEA_HINT = "Singapore Malaysia distributor pembekal"


@dataclass(frozen=True)
class Caps:
    max_calls: int = 1
    flash: int = 25
    fetch: int = 45


@dataclass(frozen=True)
class Trajectory:
    query: str
    category: str


class RecursionEdge(BaseModel):
    parent_sha: str
    child_sha: str
    depth: int
    query: str
    category: str
    confidence: float
    ts: str


class VerificationProbe(BaseModel):
    child_sha: str
    query: str
    verdicts: list[str] = Field(default_factory=list)
    agree: bool = True


class SerendipityResult(BaseModel):
    root_query: str
    mode: str
    edges: list[RecursionEdge] = Field(default_factory=list)
    leaf_count: int = 0
    verifications: list[VerificationProbe] = Field(default_factory=list)
    disputed: list[VerificationProbe] = Field(default_factory=list)
    cost: dict[str, int] = Field(default_factory=dict)
    within_caps: bool = True


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sample_trajectories(
    query: str, mode: str | None = None, *, llm: Callable[[str], str] | None = None
) -> list[Trajectory]:
    """S=5 diverse trajectories, one per category, reusing the T-1.2 expansion."""
    by_cat: dict[str, str] = {}
    for variant in expand_query(query, mode=mode, llm=llm):
        cat = _KIND_TO_CATEGORY.get(variant.kind)
        if cat and cat not in by_cat:
            by_cat[cat] = variant.text
    by_cat.setdefault("sea_local_language", f"{query} {_SEA_HINT}")
    return [Trajectory(query=by_cat.get(cat) or query, category=cat) for cat in CATEGORIES]


def run_serendipity(
    query: str,
    *,
    mode: str = "auto",
    fetch_fn: Callable[[str], object] | None = None,
    verify_fn: Callable[[str, str], str] | None = None,
    disputed_handler: Callable[[VerificationProbe], None] | None = None,
    llm: Callable[[str], str] | None = None,
    caps: Caps | None = None,
    d_max: int = DEPTH_MAX,
    width: int = WIDTH_S,
    ts: str | None = None,
) -> SerendipityResult:
    caps = caps or Caps()
    ts = ts or _now_iso()
    spent = {"max_calls": 1, "flash": 0, "fetch": 0}  # 1 top-level qwen-max planning call

    edges: list[RecursionEdge] = []
    leaves: list[tuple[str, str, int]] = []
    queue: list[tuple[str, str, int]] = [(_sha(query), query, 0)]

    while queue:
        parent_sha, parent_query, depth = queue.pop(0)
        # Stop before crossing any cap: no flash unless at least one fetch can follow.
        if depth >= d_max or spent["flash"] + 1 > caps.flash or spent["fetch"] + 1 > caps.fetch:
            leaves.append((parent_sha, parent_query, depth))
            continue
        spent["flash"] += 1
        spawned = 0
        for traj in sample_trajectories(parent_query, mode, llm=llm)[:width]:
            if spent["fetch"] + 1 > caps.fetch:
                break
            spent["fetch"] += 1
            if fetch_fn is not None:
                try:
                    fetch_fn(traj.query)
                except Exception:
                    pass
            child_sha = _sha(f"{parent_sha}:{depth + 1}:{traj.query}")
            edges.append(RecursionEdge(
                parent_sha=parent_sha, child_sha=child_sha, depth=depth + 1,
                query=traj.query, category=traj.category,
                confidence=round(HOP_DECAY ** (depth + 1), 4), ts=ts,
            ))
            queue.append((child_sha, traj.query, depth + 1))
            spawned += 1
        if spawned == 0:
            leaves.append((parent_sha, parent_query, depth))

    verifications: list[VerificationProbe] = []
    disputed: list[VerificationProbe] = []
    for child_sha, leaf_query, _depth in leaves:
        if spent["fetch"] + VERIFY_K > caps.fetch:
            break
        verdicts = []
        for lens in VERIFY_LENSES:
            spent["fetch"] += 1
            verdicts.append(verify_fn(leaf_query, lens) if verify_fn else "active")
        probe = VerificationProbe(
            child_sha=child_sha, query=leaf_query, verdicts=verdicts,
            agree=len(set(verdicts)) == 1,
        )
        verifications.append(probe)
        if not probe.agree:
            disputed.append(probe)
            if disputed_handler is not None:
                disputed_handler(probe)

    within = (spent["max_calls"] <= caps.max_calls
              and spent["flash"] <= caps.flash
              and spent["fetch"] <= caps.fetch)
    return SerendipityResult(
        root_query=query, mode=mode, edges=edges, leaf_count=len(leaves),
        verifications=verifications, disputed=disputed, cost=spent, within_caps=within,
    )
