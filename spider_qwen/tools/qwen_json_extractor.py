"""Optional Qwen JSON extraction over already-fetched page text.

This provider does not replace deterministic extractors. It enriches them when
enabled, and callers must keep deterministic fallback behavior on any error.
"""

from __future__ import annotations

import json
import os
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..extraction.contact import ContactExtractor
from ..extraction.pricing import PricingExtractor
from ..extraction.quote_channel import QuoteChannelExtractor
from ..modes.contracts import PricingStatus, PrivacyClass, QuoteChannelType
from ..observability.usage import RecordsTokenUsage
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
    # Forbid extras at the top level: a gateway envelope like {"data": {...}}
    # must fail validation (and be unwrapped), not silently parse as an empty
    # extraction with every field defaulted.
    model_config = ConfigDict(extra="forbid")

    pricing: QwenPricingExtraction = Field(default_factory=QwenPricingExtraction)
    quote_channels: list[QwenQuoteChannelExtraction] = Field(default_factory=list)
    contacts: list[QwenContactExtraction] = Field(default_factory=list)
    claims: list[QwenClaim] = Field(default_factory=list)


class QwenJsonExtractor(RecordsTokenUsage):
    """Extract procurement facts from fetched text using Qwen JSON Schema mode."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        client: Any | None = None,
        max_retries: int = 2,
    ) -> None:
        self.api_key = api_key or os.getenv("DASHSCOPE_API_KEY", "")
        self.base_url = base_url or os.getenv(
            "QWEN_BASE_URL",
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        )
        self.model = model or os.getenv("QWEN_JSON_EXTRACTOR_MODEL", "qwen-flash")
        self._client = client
        self.max_retries = max(0, int(max_retries))
        # Retry telemetry: T-2.1 acceptance is "malformed retry rate drops to ~0".
        self.calls = 0  # extract() invocations that reached the model
        self.retries = 0  # re-prompts triggered by malformed/non-conforming output
        self.malformed_final = 0  # extractions still invalid after all retries
        self.last_attempts = 0  # model calls used by the most recent extract()

    def retry_rate(self) -> float:
        """Fraction of model-reaching extractions still malformed after retries."""
        return round(self.malformed_final / self.calls, 4) if self.calls else 0.0

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
        self.calls += 1
        self.last_attempts = 0
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
        # Schema-constrained extraction: on non-conforming output, re-prompt with
        # the validation error so the malformed retry rate drops to ~0 (T-2.1).
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self.last_attempts += 1
            try:
                response = client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    extra_body={"enable_thinking": False},
                )
            except Exception as exc:  # pragma: no cover - network path
                # Transport failure is not a schema problem; do not retry-loop here.
                raise QwenJsonExtractorError(f"Qwen JSON extraction failed for {page_url}: {exc}") from exc

            self._record_usage(response)
            raw = _completion_content(response)
            try:
                return QwenPageExtraction.model_validate_json(raw)
            except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                unwrapped = _maybe_unwrap_envelope(raw)
                if unwrapped is not None:
                    try:
                        return QwenPageExtraction.model_validate(unwrapped)
                    except ValidationError:
                        pass
                last_exc = exc
                if attempt < self.max_retries:
                    self.retries += 1
                    messages = messages + [
                        {"role": "assistant", "content": raw[:2000]},
                        {
                            "role": "user",
                            "content": (
                                "That response was not valid JSON for the schema. "
                                f"Error: {exc}. Return ONLY a single valid JSON object that "
                                "conforms to the schema above. No prose, no code fences."
                            ),
                        },
                    ]
                    continue

        self.malformed_final += 1
        raise QwenJsonExtractorError(
            f"Qwen JSON extraction returned invalid schema after {self.last_attempts} "
            f"attempts for {page_url}: {last_exc}"
        )


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


def _maybe_unwrap_envelope(raw: str) -> dict[str, Any] | None:
    """Unwrap a single-key envelope like ``{"data": {...}}`` around the payload.

    Some OpenAI-compatible gateways wrap the JSON object the model produced.
    Only a dict wrapped in exactly one key holding a dict is unwrapped; anything
    else stays a schema error so the retry loop re-prompts the model.
    """
    try:
        parsed = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return None
    if isinstance(parsed, dict) and len(parsed) == 1:
        inner = next(iter(parsed.values()))
        if isinstance(inner, dict):
            return inner
    return None


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
