"""Optional Qwen tool-calling router for low-confidence classifications."""

from __future__ import annotations

import json
import os
from typing import Any

from .classifier import ClassificationResult
from .contracts import ProcurementMode


class QwenModeRouterError(Exception):
    pass


class QwenModeRouter:
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
        self.model = model or os.getenv("QWEN_ROUTER_MODEL", "qwen3.7-max")
        self._client = client

    @property
    def is_available(self) -> bool:
        return bool(self.api_key or self._client is not None)

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        if not self.api_key:
            raise QwenModeRouterError("DASHSCOPE_API_KEY not set; Qwen router unavailable")
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - optional dep
            raise QwenModeRouterError(
                "openai package not installed. Install with: pip install 'spider-qwen[qwen]'"
            ) from exc
        self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        return self._client

    def classify(self, query: str) -> ClassificationResult:
        client = self._ensure_client()
        tool = {
            "type": "function",
            "function": {
                "name": "select_procurement_mode",
                "description": "Select the procurement mode for a buyer query.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "mode": {
                            "type": "string",
                            "enum": [m.value for m in ProcurementMode],
                        },
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "rationale": {"type": "string"},
                    },
                    "required": ["mode", "confidence", "rationale"],
                    "additionalProperties": False,
                },
            },
        }
        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Classify procurement queries. Use product_exact_price for countable goods "
                            "with public price intent, service_quote_required for service RFQ sourcing, "
                            "contact_enrichment_only for contact lookup, and revalidation for refresh/verify requests."
                        ),
                    },
                    {"role": "user", "content": query},
                ],
                tools=[tool],
                tool_choice={"type": "function", "function": {"name": "select_procurement_mode"}},
                extra_body={"enable_thinking": False},
            )
        except Exception as exc:  # pragma: no cover - network path
            raise QwenModeRouterError(f"Qwen mode routing failed: {exc}") from exc

        data = _tool_arguments(response)
        try:
            return ClassificationResult(
                mode=ProcurementMode(data["mode"]),
                confidence=round(float(data.get("confidence", 0.65)), 2),
                signals={"qwen_tool_call": ["select_procurement_mode"]},
                rationale=f"qwen router: {data.get('rationale', '').strip()}",
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise QwenModeRouterError(f"Qwen router returned invalid arguments: {data}") from exc


def _tool_arguments(response: Any) -> dict[str, Any]:
    choices = getattr(response, "choices", None) or []
    if not choices:
        raise QwenModeRouterError("Qwen router response had no choices")
    message = getattr(choices[0], "message", None)
    tool_calls = getattr(message, "tool_calls", None) or []
    if tool_calls:
        fn = getattr(tool_calls[0], "function", None)
        raw = getattr(fn, "arguments", None)
        if raw:
            return json.loads(raw)
    content = getattr(message, "content", None)
    if content:
        return json.loads(content)
    raise QwenModeRouterError("Qwen router response had no tool call or JSON content")
