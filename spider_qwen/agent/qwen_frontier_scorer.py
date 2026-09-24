"""Qwen frontier re-scorer: the planner seam over the lead queue.

Qwen proposes per-lead score deltas ("this directory listing is worth more
than its prior suggests; this blog post less"); the deterministic frontier
clamps every delta to +/- MAX_SCORER_DELTA and keeps admission authority -- a
lead Qwen dislikes is reordered, never removed, and a lead below the score
floor is never resurrected. Any failure degrades to the deterministic order.
"""

from __future__ import annotations

import os
import re
from typing import Any

from ..observability.usage import RecordsTokenUsage

_SYSTEM = (
    "You prioritize a procurement research crawl. Given the buyer query and a "
    "numbered list of candidate URLs with their current scores, reply with "
    "one adjustment per line in the form 'INDEX DELTA' (DELTA between -0.2 "
    "and 0.2), only for leads whose priority should change. Boost official "
    "vendor pages, supplier directories, and RFQ/contact pages; demote blog "
    "posts, news, and listicles. No commentary."
)

_DELTA_LINE_RE = re.compile(r"^\s*(\d+)\s+(-?\d*\.?\d+)\s*$")


class QwenFrontierScorerError(Exception):
    pass


class QwenFrontierScorer(RecordsTokenUsage):
    """Callable ``scorer(query, leads) -> {url: delta}`` over frontier url leads.

    ``leads`` is a list of ``(url, score, provenance)`` tuples.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("DASHSCOPE_API_KEY", "")
        self.base_url = base_url or os.getenv(
            "QWEN_BASE_URL",
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        )
        self.model = model or os.getenv("QWEN_FRONTIER_SCORER_MODEL", "qwen-flash")
        self._client = client

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if not self.api_key:
            raise QwenFrontierScorerError("DASHSCOPE_API_KEY not set; Qwen frontier scorer unavailable")
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - optional dep
            raise QwenFrontierScorerError(
                "openai package not installed. Install with: pip install 'spider-qwen[qwen]'"
            ) from exc
        from ..tools.qwen_timeouts import MAX_RETRIES, qwen_timeout_seconds

        self._client = OpenAI(
            api_key=self.api_key, base_url=self.base_url,
            timeout=qwen_timeout_seconds(), max_retries=MAX_RETRIES,
        )
        return self._client

    def __call__(self, query: str, leads: list[tuple[str, float, str]]) -> dict[str, float]:
        client = self._ensure_client()
        lines = "\n".join(
            f"{i}. {url} (score {score:.2f}, via {provenance})"
            for i, (url, score, provenance) in enumerate(leads)
        )
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": f"Buyer query: '{query}'\nLeads:\n{lines}"},
            ],
            extra_body={"enable_thinking": False},
        )
        self._record_usage(response)
        choices = getattr(response, "choices", None) or []
        content = getattr(getattr(choices[0], "message", None), "content", None) if choices else None
        return parse_deltas(content or "", leads)


def parse_deltas(text: str, leads: list[tuple[str, float, str]]) -> dict[str, float]:
    """'INDEX DELTA' lines -> {url: delta}; malformed lines are ignored."""
    deltas: dict[str, float] = {}
    for line in (text or "").splitlines():
        m = _DELTA_LINE_RE.match(line)
        if not m:
            continue
        idx = int(m.group(1))
        if 0 <= idx < len(leads):
            deltas[leads[idx][0]] = float(m.group(2))
    return deltas


class MockQwenFrontierScorer:
    """Deterministic offline stand-in: boosts contact/RFQ-shaped URLs."""

    model = "mock"

    def __call__(self, query: str, leads: list[tuple[str, float, str]]) -> dict[str, float]:
        return {
            url: 0.1
            for url, _score, _provenance in leads
            if re.search(r"contact|quote|rfq", url, re.IGNORECASE)
        }
