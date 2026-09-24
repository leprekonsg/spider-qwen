"""Qwen seams fail closed: bounded requests, malformed replies, provider allowlist."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from spider_qwen.agent.tool_registry import ToolRegistry
from spider_qwen.modes.qwen_router import QwenModeRouterError, _tool_arguments
from spider_qwen.tools.qwen_timeouts import qwen_timeout_seconds


def _response(content=None, arguments=None):
    tool_calls = [SimpleNamespace(function=SimpleNamespace(arguments=arguments))] if arguments else []
    message = SimpleNamespace(tool_calls=tool_calls, content=content)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


@pytest.mark.parametrize("response", [
    _response(arguments="{not json"),
    _response(content="Sure! The mode is service."),
    _response(arguments='["service_quote_required"]'),
])
def test_router_malformed_reply_is_a_router_error(response):
    with pytest.raises(QwenModeRouterError):
        _tool_arguments(response)


def test_controller_falls_back_to_classifier_on_malformed_router_reply():
    from spider_qwen.agent.controller import Controller

    class _GarbageRouter:
        is_available = True

        def classify(self, query):
            return _tool_arguments(_response(content="{broken"))

    controller = Controller(offline=True, qwen_router=_GarbageRouter(), state_dir=None, persist=False)
    result = asyncio.run(controller.run("find something for our office", mode="auto"))
    # The run completed on the deterministic classifier.
    assert "qwen router fallback failed" in result.classification.rationale


def test_qwen_timeout_defaults_and_env_override(monkeypatch):
    monkeypatch.delenv("QWEN_REQUEST_TIMEOUT_SECONDS", raising=False)
    assert qwen_timeout_seconds() == 60.0
    assert qwen_timeout_seconds("web_extractor") == 120.0
    monkeypatch.setenv("QWEN_REQUEST_TIMEOUT_SECONDS", "15")
    assert qwen_timeout_seconds("web_extractor") == 15.0
    monkeypatch.setenv("QWEN_REQUEST_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValueError, match="must be positive"):
        qwen_timeout_seconds()


def test_tool_registry_checks_the_provider_behind_a_tool():
    ToolRegistry.require_allowed("fetch", "tinyfish_fetch")
    ToolRegistry.require_allowed("fetch", "qwen_web_extractor")
    with pytest.raises(ValueError, match="not an allowed v1 fetch provider"):
        ToolRegistry.require_allowed("fetch", "tinyfish_browser")
    with pytest.raises(ValueError, match="not allowed in v1"):
        ToolRegistry.require_allowed("qwen_web_extractor")


def test_fetch_service_refuses_a_disallowed_provider():
    from spider_qwen.evidence.ledger import EvidenceLedger
    from spider_qwen.tools.fetch_service import FetchService

    class _BrowserAgent:
        provider_name = "tinyfish_browser"
        fetch_source_tool = "tinyfish_browser"

        async def fetch(self, urls, output_format="markdown", include_links=True):
            raise AssertionError("must not be called")

    service = FetchService(_BrowserAgent(), EvidenceLedger("run_registry"))
    with pytest.raises(ValueError, match="tinyfish_browser"):
        asyncio.run(service.fetch(["https://example.sg"]))
