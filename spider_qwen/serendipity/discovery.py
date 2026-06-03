"""T-8.2: the ``--serendipity`` discovery sidecar.

Populates the three serendipity senses from the REAL components built across the
earlier phases, over a run's already-fetched evidence ledger:

  S1 substitute              -> supplier-part graph (T-3.1/3.2) + CoVe gate (T-2.3)
  S2 archival/hidden source  -> long-tail tier sources + Wayback items (T-2.4/T-5.2)
  S3 risk watch / DMSMS      -> proactive lifecycle/PCN signals (T-5.4) + SD-22 ladder (T-6.1)

This is a discovery *sidecar*: it runs AFTER the normal pipeline on the existing
ledger, makes no new search/fetch calls (so it cannot starve verification), and
does NOT mutate ranking or RFQ drafts. The default ``run()`` is unchanged; the
controller only builds this when ``run(serendipity=True)``. Every promoted item
carries evidence_refs + a source_component; partial/unsupported discoveries are
marked, not promoted; substitutes still pass CoVe verification. Full integration
into the default pipeline is deferred to a v2 hardening pass.
"""

from __future__ import annotations

import re
from collections import deque
from typing import Any

from pydantic import BaseModel, Field

from .. import SCHEMA_VERSION
from ..evidence.models import EvidenceRef
from ..governance.source_reliability import classify_source, host_of
from ..graph.extract import find_mpns, ingest_text
from ..graph.retrieve import personalized_pagerank
from ..graph.schema import part_key
from ..graph.store import GraphStore
from ..verification.cove import ChainOfVerification, SubstituteCandidate
from .dmsms import build_dmsms_ladder
from .signals import SUBSTITUTE_RELS, detect_lifecycle, proactive_watch

# Hard caps so the post-pipeline sidecar stays bounded (it touches no fetch/search
# budget, but compute is still capped; clipping is noted, never silent).
_MAX_SUBSTITUTES = 8
_MAX_SOURCES = 8
_MAX_HOPS = 2          # substitute chains stay bounded (graph acceptance is <=2 hops)
_HOP_DECAY = 0.85      # per-hop confidence decay (matches T-3.3 GRAM-lite)
# Tiers that count as "long tail / hidden" sources beyond the authoritative ones.
_LONG_TAIL_TIERS = ("broker", "marketplace", "aggregator")
_OBSOLETE_STATES = ("eol", "ltb", "nrnd")
_SENTENCE_SPLIT = re.compile(r"[.!?\n]+")
# Substitute-relation phrases (mirror graph.extract._REL_PHRASES). When an MPN
# appears immediately AFTER one of these it is the successor/replacement, so a
# lifecycle marker in that sentence describes the predecessor, not this part.
_SUCCESSOR_PHRASES = (
    "cross-references", "cross references", "cross-reference for",
    "is superseded by", "superseded by", "replaced by",
    "pin-compatible with", "pin compatible with", "same die as", "renamed to",
)
_SUCCESSOR_RE = re.compile(
    r"(?:" + "|".join(re.escape(p) for p in _SUCCESSOR_PHRASES) + r")\s*$", re.IGNORECASE
)


class DiscoverySlotItem(BaseModel):
    schema_version: str = SCHEMA_VERSION
    kind: str  # substitute | archival_source | long_tail_source | risk_watch | dmsms
    summary: str
    source_component: str  # graph_ppr | wayback | long_tail_rank | signals | dmsms
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    detail: dict[str, Any] = Field(default_factory=dict)


class DiscoverySlot(BaseModel):
    schema_version: str = SCHEMA_VERSION
    slot: str  # S1 | S2 | S3
    kind: str  # substitute | archival_or_hidden_source | risk_watch_or_dmsms
    supported: bool = False
    note: str = ""
    items: list[DiscoverySlotItem] = Field(default_factory=list)


class DiscoveryResult(BaseModel):
    schema_version: str = SCHEMA_VERSION
    slots: list[DiscoverySlot] = Field(default_factory=list)
    within_budget: bool = True
    explanation: str = ""


def _ref(ledger, ledger_id: str) -> EvidenceRef | None:
    item = getattr(ledger, "get", lambda _x: None)(ledger_id)
    if item is None:
        return None
    return EvidenceRef(
        ledger_id=item.ledger_id,
        url=item.final_url or item.url,
        snippet_hash=item.snippet_hash,
        retrieved_at=item.retrieved_at,
    )


def _build_graph(ledger) -> GraphStore:
    store = GraphStore()
    for item in ledger.items():
        if item.text:
            ingest_text(store, item.text, evidence_claim_id=item.ledger_id, reliability=item.reliability)
    return store


def _local_lifecycle(text: str, mpn: str) -> str:
    """Lifecycle state asserted ABOUT ``mpn``, not merely the sentence it sits in.

    Word-boundary matched (so a seed MPN that prefixes the substitute -- NE5532 in
    NE5532A -- cannot taint it), and occurrences where ``mpn`` is the OBJECT of a
    supersession/cross-reference phrase are skipped: a lifecycle marker there
    ("Obsolete NE5532 is superseded by NE5532A") describes the predecessor being
    replaced, not the healthy substitute."""
    token = re.compile(r"\b" + re.escape(mpn) + r"\b", re.IGNORECASE)
    for sentence in _SENTENCE_SPLIT.split(text or ""):
        for m in token.finditer(sentence):
            if _SUCCESSOR_RE.search(sentence[: m.start()]):
                continue  # mpn is the replacement here; marker belongs to the predecessor
            state = detect_lifecycle(sentence)
            if state != "unknown":
                return state
    return "unknown"


def _substitute_adjacency(store: GraphStore) -> dict[str, list[tuple[str, float]]]:
    """Adjacency over substitute relations only, weighted confidence x reliability,
    so PPR ranks reachable substitutes by seed-proximity (not by unrelated
    manufacturer/distributor edges)."""
    adj: dict[str, list[tuple[str, float]]] = {}
    for e in store.edges():
        if e["rel"] in SUBSTITUTE_RELS:
            adj.setdefault(e["src"], []).append(
                (e["dst"], float(e["confidence"]) * float(e["reliability"])))
    return adj


def _walk_substitutes(store: GraphStore, seeds: list[str]) -> dict[str, dict[str, Any]]:
    """Multi-hop BFS over substitute relations from each seed, so a chain
    NE5532 -> NE5532A -> LM358 surfaces LM358, not just the direct neighbour.
    Returns the best (shallowest, then highest-confidence) reachable substitute
    per MPN, each with its asserting-edge evidence, relation, hop depth, per-hop
    decayed confidence, and a human-readable path."""
    seed_keys = {part_key(s) for s in seeds}
    found: dict[str, dict[str, Any]] = {}
    for seed in seeds:
        queue: deque[tuple[str, int, str]] = deque([(part_key(seed), 0, seed.upper())])
        seen = {part_key(seed)}
        while queue:
            node, depth, path = queue.popleft()
            if depth >= _MAX_HOPS:
                continue
            for edge in store.neighbors(node, rels=SUBSTITUTE_RELS):
                dst = edge["dst"]
                sub_mpn = (dst.split(":", 1)[1] if ":" in dst else dst).upper()
                hop_depth = depth + 1
                conf = float(edge["confidence"]) * (_HOP_DECAY ** depth)
                new_path = f"{path} -> {edge['rel']} -> {sub_mpn}"
                prev = found.get(sub_mpn)
                if dst not in seed_keys and (
                    prev is None
                    or hop_depth < prev["depth"]
                    or (hop_depth == prev["depth"] and conf > prev["confidence"])
                ):
                    found[sub_mpn] = {
                        "relation": edge["rel"], "confidence": conf,
                        "evidence_claim_id": edge["evidence_claim_id"],
                        "depth": hop_depth, "seed": seed, "path": new_path,
                    }
                if dst not in seen:
                    seen.add(dst)
                    queue.append((dst, hop_depth, new_path))
    return found


def _discover_substitutes(query: str, ledger, store: GraphStore, seeds: list[str], text: str, cove) -> DiscoverySlot:
    found = _walk_substitutes(store, seeds)
    # PPR over the substitute subgraph orders candidates by seed-proximity (T-3.2).
    scores = (personalized_pagerank(_substitute_adjacency(store), [part_key(s) for s in seeds])
              if seeds else {})
    ordered = sorted(found.items(), key=lambda kv: scores.get(part_key(kv[0]), 0.0), reverse=True)

    drafts: list[SubstituteCandidate] = []
    meta: dict[str, dict[str, Any]] = {}
    for sub_mpn, info in ordered:
        ref = _ref(ledger, info["evidence_claim_id"])
        refs = [ref] if ref else []
        drafts.append(SubstituteCandidate(
            mpn=sub_mpn, lifecycle_status=_local_lifecycle(text, sub_mpn),
            confidence=info["confidence"], rationale=info["path"], evidence_refs=refs,
        ))
        meta[sub_mpn] = info

    result = (cove or ChainOfVerification()).verify(drafts)
    items: list[DiscoverySlotItem] = []
    for cand in result.verified[:_MAX_SUBSTITUTES]:
        m = meta.get(cand.mpn, {})
        items.append(DiscoverySlotItem(
            kind="substitute",
            summary=f"{cand.mpn} (via {m.get('relation', 'CROSS_REFERENCE')}, {m.get('depth', 1)}-hop)",
            source_component="graph_ppr", evidence_refs=cand.evidence_refs,
            detail={"mpn": cand.mpn, "relation": m.get("relation", ""),
                    "confidence": round(float(m.get("confidence", 0.0)), 4),
                    "lifecycle": cand.lifecycle_status, "seed": m.get("seed", ""),
                    "depth": m.get("depth", 0), "path": m.get("path", "")},
        ))
    notes = []
    if result.removed:
        notes.append(f"{len(result.removed)} candidate(s) failed CoVe verification (dead lifecycle or no evidence) and were not promoted.")
    if len(result.verified) > _MAX_SUBSTITUTES:
        notes.append(f"capped at {_MAX_SUBSTITUTES} of {len(result.verified)} verified substitutes.")
    if not seeds:
        notes.append("no MPN seed in the query; substitute discovery needs a part number.")
    return DiscoverySlot(slot="S1", kind="substitute", supported=bool(items), note=" ".join(notes), items=items)


def _discover_sources(ledger) -> DiscoverySlot:
    candidates: list[DiscoverySlotItem] = []
    seen_hosts: set[tuple[str, bool]] = set()
    for item in ledger.items():
        url = item.final_url or item.url
        host = host_of(url)
        tier = classify_source(host)
        archival = item.source_tool == "wayback_cdx"
        if not archival and tier not in _LONG_TAIL_TIERS:
            continue
        key = (host, archival)
        if key in seen_hosts:  # one entry per distinct source host (dedupe expansion noise)
            continue
        ref = _ref(ledger, item.ledger_id)
        if ref is None:
            continue
        seen_hosts.add(key)
        candidates.append(DiscoverySlotItem(
            kind="archival_source" if archival else "long_tail_source",
            summary=f"{host} ({'wayback archive' if archival else tier})",
            source_component="wayback" if archival else "long_tail_rank",
            evidence_refs=[ref], detail={"host": host, "tier": tier, "archival": archival},
        ))
    items = candidates[:_MAX_SOURCES]
    notes = []
    if len(candidates) > _MAX_SOURCES:  # clipping is noted, never silent
        notes.append(f"capped at {_MAX_SOURCES} of {len(candidates)} long-tail/archival sources.")
    if not items:
        notes.append("no long-tail/broker or archived sources in this run's ledger.")
    return DiscoverySlot(slot="S2", kind="archival_or_hidden_source", supported=bool(items),
                         note=" ".join(notes), items=items)


def _asserting_refs(ledger, seed: str) -> list[EvidenceRef]:
    """Refs of the pages that actually mention ``seed`` with a lifecycle/PCN signal."""
    low = seed.lower()
    refs = []
    for it in ledger.items():
        body = (it.text or "").lower()
        if low in body and (detect_lifecycle(body) != "unknown" or "pcn" in body or "pdn" in body):
            ref = _ref(ledger, it.ledger_id)
            if ref is not None:
                refs.append(ref)
    return refs


def _discover_risk(ledger, store: GraphStore, seeds: list[str], text: str) -> DiscoverySlot:
    items: list[DiscoverySlotItem] = []
    for seed in seeds:
        all_refs = _asserting_refs(ledger, seed)
        watch = proactive_watch(seed, text, store, evidence_refs=all_refs)
        if not watch.triggered:
            continue
        if watch.risk_signal is not None:
            items.append(DiscoverySlotItem(
                kind="risk_watch", summary=watch.risk_signal.description or f"{seed} lifecycle risk",
                source_component="signals", evidence_refs=list(watch.risk_signal.evidence_refs) or all_refs,
                detail={"mpn": seed, "lifecycle": watch.lifecycle_status,
                        "signal_type": watch.risk_signal.signal_type,
                        "alternates": [a.mpn for a in watch.alternates]},
            ))
        if watch.lifecycle_status in _OBSOLETE_STATES:
            ladder = build_dmsms_ladder(
                seed, lifecycle_status=watch.lifecycle_status,
                alternates=[a.mpn for a in watch.alternates], evidence_refs=all_refs,
            )
            applicable = [r.strategy for r in ladder.rungs if r.applicable]
            items.append(DiscoverySlotItem(
                kind="dmsms", summary=f"SD-22 ladder for {seed} ({watch.lifecycle_status})",
                source_component="dmsms", evidence_refs=list(ladder.evidence_refs) or all_refs,
                detail={"mpn": seed, "lifecycle": watch.lifecycle_status,
                        "applicable_rungs": applicable, "rungs": len(ladder.rungs)},
            ))
    note = "" if items else "no NRND/EOL/PCN lifecycle risk detected for the query part(s)."
    return DiscoverySlot(slot="S3", kind="risk_watch_or_dmsms", supported=bool(items), note=note, items=items)


def build_discovery(query: str, ledger, *, mode: str = "", cove: ChainOfVerification | None = None) -> DiscoveryResult:
    """Build the S1/S2/S3 discovery sidecar over a run's evidence ledger.

    Deterministic + offline: no new search/fetch is performed, so the normal
    pipeline's verification budget is never starved.
    """
    store = _build_graph(ledger)
    try:
        text = "\n".join(it.text for it in ledger.items() if it.text)
        seeds = find_mpns(query) or find_mpns(text)
        s1 = _discover_substitutes(query, ledger, store, seeds, text, cove)
        s2 = _discover_sources(ledger)
        s3 = _discover_risk(ledger, store, seeds, text)
    finally:
        store.close()  # exception-safe: never leak the SQLite connection

    supported = [s.slot for s in (s1, s2, s3) if s.supported]
    explanation = (
        f"Discovery sidecar over {len(ledger.items())} ledger page(s); "
        f"seeds={seeds or 'none'}; supported={supported or 'none'}. "
        "Runs after the normal pipeline on already-fetched evidence (no new "
        "search/fetch budget), so verification is never starved; substitutes pass CoVe."
    )
    return DiscoveryResult(slots=[s1, s2, s3], within_budget=True, explanation=explanation)
