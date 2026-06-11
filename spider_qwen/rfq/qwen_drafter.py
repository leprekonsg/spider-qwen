"""Qwen RFQ body drafter (.qwen/skills/sea-rfq-draft) + deterministic mock.

Chain-of-Verification split (Dhuliawala et al. 2023, arXiv:2309.11495): the
model drafts, then an independent verification step checks the draft. Here the
verify step is deterministic -- ``factcheck.unsourced_numeric_claims`` flags
quantitative claims in the drafted body that no ledger evidence supports. The
draft is never sent; hard stops and the audit log are untouched.
"""

from __future__ import annotations

import json
import os
from typing import Any

from pydantic import BaseModel

from ..observability.usage import RecordsTokenUsage

# Condensed from .qwen/skills/sea-rfq-draft/SKILL.md: grounded slots only,
# nothing invented, draft-only, SEA-market-neutral tone with a language tag.
_SYSTEM = (
    "Draft an RFQ email a buyer can review and send themselves; nothing is "
    "ever sent automatically. Use ONLY the vendor facts supplied; never invent "
    "prices, SLAs, dates, or contact details -- ask the vendor instead. Tone: "
    "SEA-market-neutral professional English, short, direct, polite. "
    'Reply as JSON: {"body": "<email text>", "language": "en-SG-neutral"}.'
)


class QwenRfqDraft(BaseModel):
    body: str
    language: str = "en-SG-neutral"


class QwenRfqDrafterError(Exception):
    pass


class QwenRfqDrafter(RecordsTokenUsage):
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
        self.model = model or os.getenv("QWEN_RFQ_DRAFTER_MODEL", "qwen3.7-max")
        self._client = client

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if not self.api_key:
            raise QwenRfqDrafterError("DASHSCOPE_API_KEY not set; Qwen RFQ drafter unavailable")
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - optional dep
            raise QwenRfqDrafterError(
                "openai package not installed. Install with: pip install 'spider-qwen[qwen]'"
            ) from exc
        self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        return self._client

    def draft(
        self,
        *,
        query: str,
        vendor_name: str,
        country: str | None = None,
        quote_channel: str | None = None,
    ) -> QwenRfqDraft:
        client = self._ensure_client()
        facts = {
            "requested_service": query,
            "vendor_name": vendor_name,
            "vendor_country": country,
            "quote_channel": quote_channel,
        }
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": json.dumps({k: v for k, v in facts.items() if v})},
            ],
            response_format={"type": "json_object"},
            extra_body={"enable_thinking": False},
        )
        self._record_usage(response)
        choices = getattr(response, "choices", None) or []
        content = getattr(getattr(choices[0], "message", None), "content", None) if choices else None
        if not content:
            raise QwenRfqDrafterError("Qwen RFQ drafter returned no content")
        return QwenRfqDraft.model_validate(json.loads(content))


class MockQwenRfqDrafter:
    """Deterministic offline stand-in. Same output shape, no numeric claims."""

    model = "mock"

    def draft(
        self,
        *,
        query: str,
        vendor_name: str,
        country: str | None = None,
        quote_channel: str | None = None,
    ) -> QwenRfqDraft:
        market = f" We operate in {country} and would appreciate market-standard terms." if country else ""
        body = (
            f"Dear {vendor_name} Team,\n\n"
            f"We are seeking a quotation for: {query}.{market}\n\n"
            "Could you share your pricing basis, scope of work, service schedule, "
            "and standard lead time? Please also indicate any minimum contract "
            "conditions and the information you need from us to quote.\n\n"
            "Thank you,\nProcurement Team"
        )
        return QwenRfqDraft(body=body, language="en-SG-neutral")
