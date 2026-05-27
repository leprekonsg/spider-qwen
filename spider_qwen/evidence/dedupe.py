"""Evidence dedupe: exact text/snippet hash, URL canonicalization, near-dup."""

from __future__ import annotations

from difflib import SequenceMatcher
from urllib.parse import urlsplit, urlunsplit

from .models import EvidenceItem

NEAR_DUPLICATE_THRESHOLD = 0.92
_TRACKING_PREFIXES = ("utm_", "gclid", "fbclid", "mc_", "ref")


def canonicalize_url(url: str) -> str:
    """Normalize scheme/host/trailing slash and strip tracking query params."""
    if not url:
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip()
    # Normalize scheme to https so http/https variants of one page collapse.
    scheme = "https"
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/") or "/"
    kept = [
        seg
        for seg in parts.query.split("&")
        if seg and not seg.lower().startswith(_TRACKING_PREFIXES)
    ]
    query = "&".join(sorted(kept))
    return urlunsplit((scheme, host, path, query, ""))


def is_near_duplicate(a: str, b: str, threshold: float = NEAR_DUPLICATE_THRESHOLD) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    return SequenceMatcher(None, a, b).ratio() >= threshold


def dedupe_items(items: list[EvidenceItem]) -> list[EvidenceItem]:
    """Drop exact text/snippet-hash duplicates and near-duplicate snippets.

    First occurrence wins. URL canonicalization collapses tracking-param and
    trailing-slash variants of the same page.
    """
    kept: list[EvidenceItem] = []
    seen_text: set[str] = set()
    seen_snippet: set[str] = set()
    seen_url: set[str] = set()

    for item in items:
        if item.text_hash and item.text_hash in seen_text:
            continue
        if item.snippet_hash and item.snippet_hash in seen_snippet:
            continue
        canon = canonicalize_url(item.final_url or item.url)
        if canon and canon in seen_url:
            continue
        if any(is_near_duplicate(item.snippet, k.snippet) for k in kept):
            continue

        kept.append(item)
        if item.text_hash:
            seen_text.add(item.text_hash)
        if item.snippet_hash:
            seen_snippet.add(item.snippet_hash)
        if canon:
            seen_url.add(canon)
    return kept
