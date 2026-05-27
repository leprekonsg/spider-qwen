"""Candidate dedupe by registrable domain, falling back to vendor name.

When two candidates collapse, the one with more evidence_refs is kept.
"""

from __future__ import annotations

from urllib.parse import urlparse


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
    name = (getattr(candidate, "vendor_name", "") or "").strip().lower()
    return f"n:{name}"


def dedupe_candidates(candidates: list) -> list:
    """Collapse duplicate vendors; keep the candidate with the most evidence."""
    best: dict[str, object] = {}
    for cand in candidates:
        key = _key(cand)
        existing = best.get(key)
        if existing is None:
            best[key] = cand
            continue
        if len(getattr(cand, "evidence_refs", [])) > len(getattr(existing, "evidence_refs", [])):
            best[key] = cand
    return list(best.values())
