"""Candidate dedupe by registrable domain, falling back to vendor name.

When two candidates collapse, the one with more evidence_refs is kept (richer
candidate wins). Evidence refs are NOT unioned — the richer candidate already
holds the superset in practice; if that assumption ever changes, union here.

Legal-name normalization: case-fold, strip punctuation, strip trailing
corporate-suffix tokens so name variants like "ORIGIN Exterminators Pte. Ltd."
and "ORIGIN Exterminators" map to the same key.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

# Trailing corporate-suffix tokens to strip (order matters: longer first so
# "pte ltd" is consumed as a unit before "ltd" would re-fire).
_SUFFIX_TOKENS: list[tuple[str, ...]] = [
    ("pte", "ltd"),
    ("sdn", "bhd"),
    ("pvt", "ltd"),
    ("private", "limited"),
    ("incorporated",),
    ("corporation",),
    ("company",),
    ("limited",),
    ("gmbh",),
    ("corp",),
    ("inc",),
    ("llp",),
    ("llc",),
    ("plc",),
    ("ltd",),
    ("co",),
]


def normalize_vendor_name(name: str) -> str:
    """Return a canonical, suffix-stripped key for a vendor name.

    Rules (applied in order):
    1. Case-fold.
    2. Strip all punctuation (replace with space).
    3. Tokenize on whitespace.
    4. Strip *trailing* corporate-suffix tokens (possibly more than one pass,
       e.g. "pte" then "ltd" or both together).
    5. Collapse whitespace.

    A name that reduces to an empty string (e.g. "Pte Ltd") returns "" — the
    caller is responsible for NOT using an empty key for deduplication.
    """
    if not name:
        return ""
    # Step 1+2: case-fold and replace punctuation with spaces.
    lowered = re.sub(r"[^a-z0-9\s]", " ", name.lower())
    tokens = lowered.split()
    if not tokens:
        return ""

    # Step 4: repeatedly strip trailing suffix sequences until none match.
    changed = True
    while changed and tokens:
        changed = False
        for suffix_seq in _SUFFIX_TOKENS:
            if len(tokens) < len(suffix_seq):
                continue
            tail = tuple(tokens[-len(suffix_seq):])
            if tail == suffix_seq:
                tokens = tokens[: -len(suffix_seq)]
                changed = True
                break  # restart from the top after any strip

    return " ".join(tokens)


def _registrable(website: str | None) -> str:
    if not website:
        return ""
    host = urlparse(website).netloc.lower() or website.lower()
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _key(candidate: object) -> str:
    domain = _registrable(getattr(candidate, "website", None))
    if domain:
        return f"d:{domain}"
    raw = (getattr(candidate, "vendor_name", "") or "").strip()
    normalized = normalize_vendor_name(raw)
    # Guard: if normalization reduced the name to nothing (suffix-only name),
    # fall back to the lowercased raw name so distinct suffix-only strings are
    # kept separate rather than collapsed into one empty-key bucket.
    key_body = normalized if normalized else raw.lower()
    return f"n:{key_body}"


def dedupe_candidates(candidates: list) -> tuple[list, int]:
    """Collapse duplicate vendors; keep the candidate with the most evidence.

    Returns (deduplicated_list, merge_count) so callers can observe merges.
    Merge policy: when two candidates share a key, keep the one with more
    evidence_refs. Tie-break: longer original vendor_name (more specific name
    is likely the richer fetch), then stable insertion order (first seen wins).
    """
    best: dict[str, object] = {}
    merge_count = 0
    for cand in candidates:
        key = _key(cand)
        existing = best.get(key)
        if existing is None:
            best[key] = cand
            continue
        # Richer candidate wins.
        cand_refs = len(getattr(cand, "evidence_refs", []) or [])
        exist_refs = len(getattr(existing, "evidence_refs", []) or [])
        if cand_refs > exist_refs:
            best[key] = cand
            merge_count += 1
        elif cand_refs == exist_refs:
            # Tie-break: longer original name (more qualified name wins).
            cand_name = (getattr(cand, "vendor_name", "") or "")
            exist_name = (getattr(existing, "vendor_name", "") or "")
            if len(cand_name) > len(exist_name):
                best[key] = cand
            merge_count += 1
        else:
            merge_count += 1
    return list(best.values()), merge_count
