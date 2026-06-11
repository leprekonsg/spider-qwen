"""Deterministic live-web fetch-outcome taxonomy.

Bot walls, JS-shell pages, geo-blocks, and genuinely empty pages are distinct
signals with distinct fixes; flattened into "insufficient evidence" they hide
why a live run starved. ``classify_page`` / ``classify_error`` name each fetch
outcome; ``FetchService`` counts them and run metrics report the histogram.

Heuristic and offline by definition: regex over the page text, no model call.
"""

from __future__ import annotations

import re

# Page outcomes (successful transport, classified by content).
PAGE_OK = "ok"
PAGE_BOT_WALL = "bot_wall"
PAGE_JS_SHELL = "js_shell"
PAGE_GEO_BLOCK = "geo_block"
PAGE_EMPTY = "empty"
PAGE_THIN = "thin"

# Error outcomes (transport / HTTP level).
ERROR_DEAD_LINK = "dead_link"
ERROR_TRANSPORT = "transport_error"

# Below this many characters a page carries too little text to extract from.
THIN_TEXT_CHARS = 100
# Interstitial walls/shells are short; above this length the signature words
# ("captcha", "javascript") are almost always incidental page content.
_INTERSTITIAL_MAX_CHARS = 2000

_BOT_WALL_RE = re.compile(
    r"captcha|cloudflare|verify (?:that )?you are (?:a )?human|access denied"
    r"|unusual traffic|are you a robot|attention required|ddos protection"
    r"|just a moment",
    re.I,
)
_JS_SHELL_RE = re.compile(
    r"enable javascript|javascript is (?:disabled|required)"
    r"|please turn on javascript|<noscript",
    re.I,
)
_GEO_BLOCK_RE = re.compile(
    r"not available in your (?:country|region|location)"
    r"|unavailable in your (?:country|region)|geo-?restricted"
    r"|blocked in your (?:country|region)",
    re.I,
)


def classify_page(text: str | None, *, title: str = "") -> str:
    """Classify a fetched page's text into one fetch outcome."""
    stripped = (text or "").strip()
    blob = f"{title or ''}\n{stripped}"
    if len(stripped) <= _INTERSTITIAL_MAX_CHARS:
        if _BOT_WALL_RE.search(blob):
            return PAGE_BOT_WALL
        if _GEO_BLOCK_RE.search(blob):
            return PAGE_GEO_BLOCK
        if not stripped:
            return PAGE_EMPTY
        if _JS_SHELL_RE.search(blob):
            return PAGE_JS_SHELL
        if len(stripped) < THIN_TEXT_CHARS:
            return PAGE_THIN
    return PAGE_OK


def classify_error(error: str = "", status: int | None = None) -> str:
    """Classify a fetch error entry (no page bytes) into one fetch outcome."""
    if status is None:
        match = re.search(r"\b([45]\d{2})\b", error or "")
        status = int(match.group(1)) if match else None
    if status in (404, 410):
        return ERROR_DEAD_LINK
    if status in (403, 429):
        return PAGE_BOT_WALL
    return ERROR_TRANSPORT
