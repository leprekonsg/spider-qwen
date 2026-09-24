"""Request bounds for Qwen OpenAI-compatible clients.

The openai SDK defaults to a 600 s timeout with 2 retries. The Qwen seams call
it synchronously inside a run, so an unbounded call outlives the run budget.
"""

from __future__ import annotations

import os

# Chat seams answer in seconds; the web_extractor also searches and thinks.
_DEFAULT_TIMEOUT_SECONDS = {"chat": 60.0, "web_extractor": 120.0}
MAX_RETRIES = 1


def qwen_timeout_seconds(kind: str = "chat") -> float:
    raw = os.getenv("QWEN_REQUEST_TIMEOUT_SECONDS")
    if raw is None:
        return _DEFAULT_TIMEOUT_SECONDS[kind]
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"QWEN_REQUEST_TIMEOUT_SECONDS={raw!r} is not a number of seconds."
        ) from exc
    if not value > 0:
        raise ValueError(f"QWEN_REQUEST_TIMEOUT_SECONDS={raw!r} must be positive.")
    return value
