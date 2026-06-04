"""Qwen WebExtractor: optional single-page fallback fetcher.

Backed by Alibaba Model Studio's responses API with the `web_extractor` tool
(OpenAI-compatible SDK, `Authorization: Bearer $DASHSCOPE_API_KEY`). TinyFish
Fetch is the default multi-URL fetcher; this is used only when Fetch is
unavailable, for single-URL Qwen-native extraction, or for benchmark
comparison.

The `openai` package is an optional dependency (extra: `qwen`). It is imported
lazily so the rest of spider-qwen runs without it.
"""

from __future__ import annotations

import asyncio
import os

from .provider_types import ExtractedPage, FetchResult, FetchResultSet


class QwenWebExtractorError(Exception):
    pass


class QwenWebExtractor:
    """Extract one webpage's content by URL via the Model Studio responses API."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("DASHSCOPE_API_KEY", "")
        self.base_url = base_url or os.getenv(
            "QWEN_BASE_URL",
            "https://dashscope-intl.aliyuncs.com/api/v2/apps/protocols/compatible-mode/v1",
        )
        self.model = model or os.getenv("QWEN_WEB_EXTRACTOR_MODEL", "qwen3.7-max")
        self._client = None

    @property
    def is_available(self) -> bool:
        return bool(self.api_key)

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if not self.api_key:
            raise QwenWebExtractorError("DASHSCOPE_API_KEY not set; WebExtractor unavailable")
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - optional dep
            raise QwenWebExtractorError(
                "openai package not installed. Install with: pip install 'spider-qwen[qwen]'"
            ) from exc
        self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        return self._client

    def extract_url_sync(self, url: str) -> ExtractedPage:
        client = self._ensure_client()
        prompt = (
            "Extract the full readable text content of this webpage and list any "
            f"contact, pricing, or quotation information found: {url}"
        )
        try:
            response = client.responses.create(
                model=self.model,
                input=prompt,
                tools=[{"type": "web_extractor"}],
                extra_body={"enable_thinking": False},
            )
        except Exception as exc:  # pragma: no cover - network path
            raise QwenWebExtractorError(f"Qwen WebExtractor call failed for {url}: {exc}") from exc
        text = getattr(response, "output_text", "") or ""
        return ExtractedPage(url=url, final_url=url, text=text, metadata={"model": self.model})

    async def extract_url(self, url: str) -> ExtractedPage:
        return await asyncio.to_thread(self.extract_url_sync, url)


class QwenWebExtractorFetchProvider:
    """Adapt the single-page WebExtractor to the FetchProvider protocol."""

    provider_name = "qwen_web_extractor"
    fetch_source_tool = "qwen_web_extractor"
    rate_limited = True  # single-page live extraction; consumes the live quota

    def __init__(self, extractor: QwenWebExtractor | None = None) -> None:
        self.extractor = extractor or QwenWebExtractor()

    async def fetch(
        self, urls: list[str], output_format: str = "markdown", include_links: bool = True
    ) -> FetchResultSet:
        results: list[FetchResult] = []
        errors: list[dict[str, str]] = []
        for url in urls:
            try:
                page = await self.extractor.extract_url(url)
                results.append(
                    FetchResult(
                        url=url,
                        final_url=page.final_url,
                        title=page.title,
                        text=page.text,
                        links=page.links,
                        source_tool="qwen_web_extractor",
                    )
                )
            except QwenWebExtractorError as exc:
                errors.append({"url": url, "error": str(exc)})
        return FetchResultSet(results=results, errors=errors, provider="qwen_web_extractor")
