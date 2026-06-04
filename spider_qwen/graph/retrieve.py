"""T-3.2: HippoRAG-style Personalized PageRank retrieval over the LPG.

Seed the restart vector on the query's part nodes, power-iterate PPR over the
edge graph (pure Python -- no scipy/numpy, fine at single-agent scale), and rank
candidate nodes by their personalized score. Each result carries a Reasoning-on-
Graphs relation path that doubles as a buyer-facing explanation
("ATMEGA48 -> SUPERSEDED_BY -> ATMEGA48A").

A deliberately flat LEXICAL baseline (`flat_lexical_rank`, a SequenceMatcher
surface-overlap ranker -- not a learned vector retriever) stands in for no-graph
retrieval; PPR beats it whenever the answer is graph-adjacent but lexically
dissimilar to the query (the multi-hop substitute case).
"""

from __future__ import annotations

from difflib import SequenceMatcher

from pydantic import BaseModel, Field

from .extract import find_mpns
from .schema import REL_TYPES, part_key


def personalized_pagerank(
    adjacency: dict[str, list[tuple[str, float]]],
    seeds: list[str],
    *,
    alpha: float = 0.15,
    iters: int = 30,
) -> dict[str, float]:
    """PPR scores over a weighted adjacency. ``alpha`` teleports back to seeds.

    ``adjacency`` maps src -> [(dst, weight)]. Mass on dangling nodes (no out
    edges) teleports back to the seed set so probability is conserved.
    """
    nodes: set[str] = set()
    for src, outs in adjacency.items():
        nodes.add(src)
        for dst, _w in outs:
            nodes.add(dst)
    nodes.update(seeds)
    if not seeds or not nodes:
        return {}

    restart = {n: 0.0 for n in nodes}
    for s in seeds:
        restart[s] += 1.0 / len(seeds)
    p = dict(restart)

    for _ in range(iters):
        new = {n: alpha * restart[n] for n in nodes}
        dangling = 0.0
        for u in nodes:
            outs = adjacency.get(u, [])
            total = sum(w for _d, w in outs)
            if total <= 0:
                dangling += (1 - alpha) * p[u]
                continue
            share = (1 - alpha) * p[u]
            for dst, w in outs:
                new[dst] += share * (w / total)
        if dangling:
            for s in seeds:
                new[s] += dangling / len(seeds)
        p = new
    return p


def flat_lexical_rank(store, query: str, *, top_k: int = 5) -> list[str]:
    """No-graph baseline: rank nodes by lexical (surface-string) similarity to the
    query. This is a lexical matcher, NOT a vector retriever -- it cannot find a
    graph-adjacent answer whose surface is lexically dissimilar to the query."""
    q = query.lower()
    scored: list[tuple[str, float]] = []
    for nid in _all_node_ids(store):
        node = store.get_node(nid)
        surface = (node["props"].get("surface") if node else None) or nid
        scored.append((nid, SequenceMatcher(None, q, str(surface).lower()).ratio()))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [nid for nid, _s in scored[:top_k]]


def _all_node_ids(store) -> list[str]:
    ids: set[str] = set()
    for edge in store.edges():
        ids.add(edge["src"])
        ids.add(edge["dst"])
    return sorted(ids)


class RetrievedNode(BaseModel):
    id: str
    score: float
    path: str = ""
    depth: int = 0


class RetrievalResult(BaseModel):
    query: str
    seeds: list[str] = Field(default_factory=list)
    results: list[RetrievedNode] = Field(default_factory=list)


class GraphRetriever:
    def __init__(self, store) -> None:
        self.store = store

    def seeds_for_query(self, query: str) -> list[str]:
        seeds = []
        for mpn in find_mpns(query):
            nid = part_key(mpn)
            if self.store.get_node(nid) is not None and nid not in seeds:
                seeds.append(nid)
        return seeds

    def _adjacency(self) -> dict[str, list[tuple[str, float]]]:
        adj: dict[str, list[tuple[str, float]]] = {}
        for e in self.store.edges():
            weight = float(e["confidence"]) * float(e["reliability"])
            adj.setdefault(e["src"], []).append((e["dst"], weight))
        return adj

    def retrieve(self, query: str, *, top_k: int = 5, max_depth: int = 3) -> RetrievalResult:
        seeds = self.seeds_for_query(query)
        if not seeds:
            return RetrievalResult(query=query)
        scores = personalized_pagerank(self._adjacency(), seeds)
        paths: dict[str, str] = {}
        depths: dict[str, int] = {}
        for s in seeds:
            for row in self.store.traverse(s, rels=REL_TYPES, max_depth=max_depth):
                paths.setdefault(row["id"], row["path"])
                depths.setdefault(row["id"], row["depth"])
        ranked = sorted(
            ((nid, sc) for nid, sc in scores.items() if nid not in seeds and sc > 0),
            key=lambda x: x[1], reverse=True,
        )[:top_k]
        results = [
            RetrievedNode(id=nid, score=round(sc, 6),
                          path=paths.get(nid, ""), depth=depths.get(nid, 0))
            for nid, sc in ranked
        ]
        return RetrievalResult(query=query, seeds=seeds, results=results)
