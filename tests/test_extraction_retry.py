"""T-2.1: schema-constrained Qwen JSON extraction with malformed-output retry.

The extractor re-prompts with the validation error when the model returns
non-conforming JSON, so the post-retry malformed rate drops to ~0. A scripted
fake client (no network) returns a fixed sequence of message contents.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from spider_qwen.tools.qwen_json_extractor import (
    QwenJsonExtractor,
    QwenJsonExtractorError,
    QwenPageExtraction,
)

_VALID = QwenPageExtraction().model_dump_json()


def _resp(content: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


class _ScriptedClient:
    """Returns the next scripted content on each chat.completions.create call."""

    def __init__(self, contents: list[str]) -> None:
        self._contents = list(contents)
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _resp(self._contents.pop(0))


def _extractor(contents, **kw):
    client = _ScriptedClient(contents)
    return QwenJsonExtractor(api_key="test", client=client, **kw), client


def test_valid_first_try_uses_no_retry():
    ex, client = _extractor([_VALID])
    out = ex.extract(text="We supply DF13 connectors. Email sales@x.sg",
                     page_url="https://x.sg", query="DF13")
    assert isinstance(out, QwenPageExtraction)
    assert ex.last_attempts == 1
    assert ex.retries == 0
    assert ex.malformed_final == 0
    assert len(client.calls) == 1


def test_malformed_then_valid_recovers_with_one_retry():
    ex, client = _extractor(["not json at all", _VALID])
    out = ex.extract(text="DF13 connector", page_url="https://x.sg", query="DF13")
    assert isinstance(out, QwenPageExtraction)
    assert ex.last_attempts == 2
    assert ex.retries == 1
    assert ex.malformed_final == 0  # recovered -> post-retry malformed rate is 0
    assert ex.retry_rate() == 0.0
    assert len(client.calls) == 2


def test_retry_prompt_includes_error_feedback():
    ex, client = _extractor(["{ broken", _VALID])
    ex.extract(text="DF13", page_url="https://x.sg", query="DF13")
    retry_blob = " ".join(m["content"] for m in client.calls[1]["messages"]).lower()
    assert "json" in retry_blob
    assert "valid" in retry_blob or "schema" in retry_blob


def test_exhausts_retries_then_raises_and_counts_failure():
    ex, client = _extractor(["bad", "still bad", "nope"], max_retries=2)
    with pytest.raises(QwenJsonExtractorError):
        ex.extract(text="DF13", page_url="https://x.sg", query="DF13")
    assert len(client.calls) == 3  # 1 attempt + 2 retries
    assert ex.retries == 2
    assert ex.malformed_final == 1
    assert ex.retry_rate() == 1.0


def test_request_is_non_thinking_json_object_with_literal_json():
    ex, client = _extractor([_VALID])
    ex.extract(text="DF13", page_url="https://x.sg", query="DF13")
    kw = client.calls[0]
    assert kw["response_format"] == {"type": "json_object"}
    assert kw["extra_body"]["enable_thinking"] is False
    prompt_blob = " ".join(m["content"] for m in kw["messages"]).lower()
    assert "json" in prompt_blob  # DashScope rejects json mode without literal "json"


def test_empty_text_short_circuits_without_calling_client():
    ex, client = _extractor([_VALID])
    out = ex.extract(text="", page_url="https://x.sg", query="DF13")
    assert isinstance(out, QwenPageExtraction)
    assert len(client.calls) == 0
    assert ex.last_attempts == 0


# --- gateway envelopes ------------------------------------------------------

def test_single_key_envelope_is_unwrapped_without_retry():
    ex, client = _extractor(['{"data": ' + _VALID + "}"])
    out = ex.extract(text="DF13 connector", page_url="https://x.sg", query="DF13")
    assert isinstance(out, QwenPageExtraction)
    assert ex.retries == 0
    assert ex.malformed_final == 0
    assert len(client.calls) == 1


def test_envelope_never_parses_as_empty_extraction():
    # extra="forbid": without unwrapping, {"data": {...}} must fail validation
    # instead of silently parsing as an all-defaults (empty) extraction.
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        QwenPageExtraction.model_validate({"data": {}, "noise": 1})


# --- token usage metering ----------------------------------------------------

class _UsageClient(_ScriptedClient):
    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=self._contents.pop(0)))],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
        )


def test_usage_is_recorded_and_drained_once():
    client = _UsageClient([_VALID])
    ex = QwenJsonExtractor(api_key="test", client=client)
    ex.extract(text="DF13", page_url="https://x.sg", query="DF13")
    assert ex.drain_usage() == [(ex.model, 100, 20)]
    assert ex.drain_usage() == []  # drain semantics: a run meters only itself
