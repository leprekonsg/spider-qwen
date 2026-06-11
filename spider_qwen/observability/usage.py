"""Per-call LLM token usage accumulation for the T-7.3 cost dashboard.

Qwen client classes mix this in and call ``_record_usage(response)`` after each
successful model call. The controller drains accumulated usage into the run's
``CostMeter`` at report time; drain semantics keep a long-lived controller from
double-counting across runs. Offline mocks never call ``_record_usage``, so the
report stays honestly "token metering unavailable".
"""

from __future__ import annotations

from typing import Any


class RecordsTokenUsage:
    """Mixin: accumulate (model, input_tokens, output_tokens) per model call."""

    _usage: list[tuple[str, int, int]]

    def _record_usage(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        if not hasattr(self, "_usage"):
            self._usage = []
        self._usage.append((
            str(getattr(self, "model", "") or "unknown"),
            int(getattr(usage, "prompt_tokens", 0) or 0),
            int(getattr(usage, "completion_tokens", 0) or 0),
        ))

    def drain_usage(self) -> list[tuple[str, int, int]]:
        """Return accumulated usage and reset, so each run meters only itself."""
        drained = getattr(self, "_usage", [])
        self._usage = []
        return drained
