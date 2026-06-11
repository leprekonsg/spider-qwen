"""Qwen corrective-query rewriter.

CRAG (Yan et al. 2024, arXiv:2401.15884) pairs its retrieval evaluator with
rewritten web-search queries when retrieval is judged off-target. Here Qwen
plays only that rewriting role: it proposes pivot queries, and the
deterministic pipeline still searches, extracts, verifies, and ranks. Any
failure degrades to the deterministic expansion variants.
"""

from __future__ import annotations

import os
import re
from typing import Any

from ..observability.usage import RecordsTokenUsage

_SYSTEM = (
    "You rewrite procurement search queries whose web retrieval came back "
    "off-target. Propose alternative queries that pivot to better procurement "
    "sources (supplier directories, RFQ portals, distributor/broker stock, "
    "manufacturer channels). Reply with one query per line, no numbering, no "
    "commentary."
)


class QwenQueryRewriterError(Exception):
    pass


class QwenQueryRewriter(RecordsTokenUsage):
    """Callable ``llm(prompt) -> str`` backend for ``corrective_queries``."""

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
        self.model = model or os.getenv("QWEN_QUERY_REWRITER_MODEL", "qwen-flash")
        self._client = client

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if not self.api_key:
            raise QwenQueryRewriterError("DASHSCOPE_API_KEY not set; Qwen query rewriter unavailable")
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - optional dep
            raise QwenQueryRewriterError(
                "openai package not installed. Install with: pip install 'spider-qwen[qwen]'"
            ) from exc
        self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        return self._client

    def __call__(self, prompt: str) -> str:
        client = self._ensure_client()
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": prompt},
            ],
            extra_body={"enable_thinking": False},
        )
        self._record_usage(response)
        choices = getattr(response, "choices", None) or []
        content = getattr(getattr(choices[0], "message", None), "content", None) if choices else None
        return content or ""


_QUOTED_QUERY_RE = re.compile(r"'([^']+)'")


class MockQwenQueryRewriter:
    """Deterministic offline stand-in: pivots derived from the prompt's query."""

    model = "mock"

    def __call__(self, prompt: str) -> str:
        m = _QUOTED_QUERY_RE.search(prompt or "")
        base = m.group(1) if m else "procurement"
        return f"{base} supplier directory\n{base} request for quotation portal"
