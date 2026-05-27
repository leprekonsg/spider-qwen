"""Optional Qwen JSON extraction over already-fetched page text.

This provider does not replace deterministic extractors. It enriches them when
enabled, and callers must keep deterministic fallback behavior on any error.
"""

from __future__ import annotations

import json
import os
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from ..extraction.contact import ContactExtractor
from ..extraction.pricing import PricingExtractor
from ..extraction.quote_channel import QuoteChannelExtractor
from ..modes.contracts import PricingStatus, PrivacyClass, QuoteChannelType
from .qwen_skills import load_skill_prompt


class QwenJsonExtractorError(Exception):
    pass


class QwenClaim(BaseModel):
    field: str
    value: str = ""
    start_char: int = -1
    end_char: int = -1
    confidence: float = 0.0


class QwenPricingExtraction(BaseModel):
    status: PricingStatus = PricingStatus.NOT_FOUND
    price: float | None = None
    currency: str | None = None
    unit: str | None = None
    matched_text: str = ""
    claims: list[QwenClaim] = Field(default_factory=list)


class QwenQuoteChannelExtraction(BaseModel):
    type: QuoteChannelType
    value: str
    matched_text: str = ""
    confidence: float = 0.0
    claims: list[QwenClaim] = Field(default_factory=list)


class QwenContactExtraction(BaseModel):
    type: str
    value: str
    matched_text: str = ""
    confidence: float = 0.0
    privacy_class: PrivacyClass = PrivacyClass.BUSINESS_CONTACT
    claims: list[QwenClaim] = Field(default_factory=list)


class QwenPageExtraction(BaseModel):
    pricing: QwenPricingExtraction = Field(default_factory=QwenPricingExtraction)
    quote_channels: list[QwenQuoteChannelExtraction] = Field(default_factory=list)
    contacts: list[QwenContactExtraction] = Field(default_factory=list)
    claims: list[QwenClaim] = Field(default_factory=list)


class QwenJsonExtractor:
    """Extract procurement facts from fetched text using Qwen JSON Schema mode."""

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
        self.model = model or os.getenv("QWEN_JSON_EXTRACTOR_MODEL", "qwen-flash")
        self._client = client

    @property
    def is_available(self) -> bool:
        return bool(self.api_key or self._client is not None)

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if not self.api_key:
            raise QwenJsonExtractorError("DASHSCOPE_API_KEY not set; Qwen JSON extraction unavailable")
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - optional dep
            raise QwenJsonExtractorError(
                "openai package not installed. Install with: pip install 'spider-qwen[qwen]'"
            ) from exc
        self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        return self._client

    def extract(self, *, text: str, page_url: str, query: str) -> QwenPageExtraction:
        if not text:
            return QwenPageExtraction()
        client = self._ensure_client()
        skill_prompt = load_skill_prompt("procurement-quote-channel")
        schema_json = json.dumps(QwenPageExtraction.model_json_schema())
        # DashScope's OpenAI-compatible mode supports response_format json_object
        # (not OpenAI's json_schema/strict). The schema is supplied in the prompt
        # and the result is validated with Pydantic. The word "json" must appear
        # in the prompt or DashScope rejects the request.
        messages = [
            {
                "role": "system",
                "content": (
                    "You extract procurement facts from fetched page text. "
                    "Return ONLY a JSON object that conforms to the provided JSON Schema. "
                    "Include character spans using Python slicing offsets "
                    "[start_char:end_char] against the provided text. "
                    "Do not invent facts. If a fact is not present, leave it empty."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Buyer query: {query}\nPage URL: {page_url}\n\n"
                    f"Qwen skill guidance:\n{skill_prompt}\n\n"
                    f"Return a JSON object matching this JSON Schema:\n{schema_json}\n\n"
                    f"Page text:\n{text[:12000]}"
                ),
            },
        ]
        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=messages,
                response_format={"type": "json_object"},
                extra_body={"enable_thinking": False},
            )
        except Exception as exc:  # pragma: no cover - network path
            raise QwenJsonExtractorError(f"Qwen JSON extraction failed for {page_url}: {exc}") from exc

        raw = _completion_content(response)
        try:
            return QwenPageExtraction.model_validate_json(raw)
        except (ValidationError, ValueError, json.JSONDecodeError) as exc:
            raise QwenJsonExtractorError(f"Qwen JSON extraction returned invalid schema: {exc}") from exc


class MockQwenJsonExtractor:
    """Deterministic stand-in used by offline demos/tests when Qwen paths are enabled."""

    def __init__(self) -> None:
        self._pricing = PricingExtractor()
        self._quote = QuoteChannelExtractor()
        self._contacts = ContactExtractor()

    def extract(self, *, text: str, page_url: str, query: str) -> QwenPageExtraction:
        pricing = self._pricing.extract(text)
        quote_channels = [
            QwenQuoteChannelExtraction(
                type=m.type,
                value=m.value,
                matched_text=m.matched_text,
                confidence=0.75,
                claims=_claims_for("quote_channel", text, m.matched_text or m.value),
            )
            for m in self._quote.extract(text, [], page_url)
        ]
        contacts = [
            QwenContactExtraction(
                type=m.type,
                value=m.value,
                matched_text=m.value,
                confidence=m.confidence,
                privacy_class=m.privacy_class,
                claims=_claims_for("contact", text, m.value),
            )
            for m in self._contacts.extract(text, [])
        ]
        pricing_claims = _claims_for("pricing", text, pricing.matched_text)
        return QwenPageExtraction(
            pricing=QwenPricingExtraction(
                status=pricing.status,
                price=pricing.price,
                currency=pricing.currency,
                unit=pricing.unit,
                matched_text=pricing.matched_text,
                claims=pricing_claims,
            ),
            quote_channels=quote_channels,
            contacts=contacts,
            claims=pricing_claims,
        )


def _completion_content(response: Any) -> str:
    choices = getattr(response, "choices", None) or []
    if choices:
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None)
        if content:
            return content
    content = getattr(response, "output_text", None)
    if content:
        return content
    raise QwenJsonExtractorError("Qwen response did not include message content")


def _claims_for(field: str, text: str, value: str) -> list[QwenClaim]:
    if not value:
        return []
    start = (text or "").lower().find(value.lower())
    if start < 0:
        return []
    end = start + len(value)
    return [QwenClaim(field=field, value=text[start:end], start_char=start, end_char=end, confidence=0.75)]
