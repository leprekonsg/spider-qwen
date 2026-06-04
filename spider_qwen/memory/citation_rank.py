"""Citation-weighted reflective memory ranking, supervised by the ledger.

RMM (ACL 2025) trains a reranker with RL using cited evidence as reward. Here
the reward signal is free and tamper-evident: when a validated candidate's
evidence chain includes a recalled ``semantic_memory`` ledger row, that fact
was *actually used* in an output, so its ``citation_count`` increments. The
recall ranker then boosts frequently-cited facts with a deterministic
multiplier - no RL, no model. Forging the signal means forging the ledger,
which breaks the SHA-256 chain and the Merkle log.

Freshness is already priced in by Ebbinghaus decay (memory/decay.py); the
multiplier covers only the usage dimension.
"""

from __future__ import annotations

import math

# Boost per doubling of citations: count 1 -> x1.2, 3 -> x1.4, 7 -> x1.6.
# Logarithmic so a runaway favorite cannot drown out fresh facts.
CITATION_WEIGHT = 0.2


def citation_multiplier(fact) -> float:
    """Deterministic recall boost from ledger-verified usage. 1.0 at zero."""
    count = max(0, int(getattr(fact, "citation_count", 0) or 0))
    return round(1.0 + CITATION_WEIGHT * math.log2(1.0 + count), 4)


def cited_fact_ids(ledger, candidates) -> set[str]:
    """Fact ids whose recalled ledger rows back the given candidates."""
    out: set[str] = set()
    for cand in candidates:
        for ref in getattr(cand, "evidence_refs", None) or []:
            item = ledger.get(getattr(ref, "ledger_id", ""))
            if item is None or item.source_tool != "semantic_memory":
                continue
            fact_id = item.metadata.get("fact_id")
            if fact_id:
                out.add(fact_id)
    return out


def record_citations(memory, ledger, candidates) -> int:
    """Increment citation counts for facts used by validated candidates.

    One citation per fact per run (set semantics), no matter how many
    candidates reused the same recalled row. Returns the number of facts
    credited.
    """
    credited = 0
    for fact_id in sorted(cited_fact_ids(ledger, candidates)):
        if memory.record_citation(fact_id):
            credited += 1
    return credited
