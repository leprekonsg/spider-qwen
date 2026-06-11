"""Deterministic fact-check of a drafted RFQ body (the CoVe verify step).

Flags quantitative claims (currency amounts, percentages, day counts) in the
drafted body that do not appear in the candidate's ledger evidence or the
buyer's own query. The check is conservative on purpose: it only inspects
number-bearing claims, the kind an LLM drafter could hallucinate with real
commercial consequences.
"""

from __future__ import annotations

import re

_NUMERIC_CLAIM_RE = re.compile(
    r"(?:S\$|US\$|RM|Rp|\$|SGD|USD|MYR|IDR|VND|THB|PHP)\s?[\d][\d.,]*"
    r"|\b\d+(?:\.\d+)?\s?%"
    r"|\b\d+\s+(?:business\s+)?(?:days?|weeks?|months?)\b",
    re.IGNORECASE,
)


def unsourced_numeric_claims(body: str, evidence_corpus: str) -> list[str]:
    """Numeric claims in ``body`` absent from ``evidence_corpus`` (order kept)."""
    corpus = " ".join((evidence_corpus or "").lower().split())
    flags: list[str] = []
    seen: set[str] = set()
    for match in _NUMERIC_CLAIM_RE.finditer(body or ""):
        token = " ".join(match.group(0).split())
        key = token.lower()
        if key in seen or key in corpus:
            continue
        seen.add(key)
        flags.append(token)
    return flags
