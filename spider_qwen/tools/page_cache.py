"""Cross-run read-through page cache keyed by canonical URL.

Two runs in the same category a day apart should not re-buy identical pages.
Fetched pages are stored one JSON file per canonical URL under
``state_dir/page_cache``; a hit inside the freshness TTL skips the provider
call and consumes no fetch budget. The cached page is still judged and
re-recorded in the new run's ledger (with cache provenance in metadata), so
every run's evidence chain stays complete and self-contained.

Only ``ok``-classified pages are cached: pinning a bot wall or JS shell for a
day would convert a transient failure into a persistent one.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from ..evidence.dedupe import canonicalize_url
from ..evidence.models import sha256_hex, utc_now_iso
from .fetch_failures import PAGE_OK, classify_page

if TYPE_CHECKING:
    from .provider_types import FetchResult

SCHEMA_VERSION = "1.0"
DEFAULT_TTL_SECONDS = 86400


class CachedPage(BaseModel):
    schema_version: str = SCHEMA_VERSION
    url: str
    final_url: str | None = None
    title: str | None = None
    text: str = ""
    links: list[str] = Field(default_factory=list)
    language: str | None = None
    source_tool: str = "tinyfish_fetch"
    fetched_at: str = Field(default_factory=utc_now_iso)

    def age_seconds(self, now_iso: str | None = None) -> float:
        now = datetime.fromisoformat(now_iso or utc_now_iso())
        return (now - datetime.fromisoformat(self.fetched_at)).total_seconds()


class PageCache:
    def __init__(self, state_dir: str | Path, *, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self.dir = Path(state_dir) / "page_cache"
        self.ttl_seconds = ttl_seconds

    def _path(self, url: str) -> Path:
        return self.dir / f"{sha256_hex(canonicalize_url(url))[:24]}.json"

    def get(self, url: str) -> CachedPage | None:
        """Fresh cached page for the URL, or None (miss, stale, unreadable)."""
        target = self._path(url)
        if not target.exists():
            return None
        try:
            page = CachedPage.model_validate_json(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if page.age_seconds() > self.ttl_seconds:
            # Stale entries stay on disk; the read-through refresh overwrites them.
            return None
        return page

    def put(self, page: "FetchResult") -> bool:
        """Cache a fetched page. Refuses non-ok pages; returns True when stored."""
        if classify_page(page.text, title=page.title or "") != PAGE_OK:
            return False
        entry = CachedPage(
            url=page.url,
            final_url=page.final_url,
            title=page.title,
            text=page.text or "",
            links=list(page.links or [])[:50],
            language=page.language,
            source_tool=page.source_tool or "tinyfish_fetch",
        )
        self.dir.mkdir(parents=True, exist_ok=True)
        self._path(page.url).write_text(entry.model_dump_json(), encoding="utf-8")
        return True
