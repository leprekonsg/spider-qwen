"""T-7.1: MCP integration.

The handlers are pure, deterministic, and dependency-free (always tested). The
FastMCP adapter is exercised only when the optional ``mcp`` SDK is installed; when
it is absent, building the server raises a clear "install spider-qwen[mcp]" error.
Live Drive / DashScope Responses-API wiring is deferred to Phase 8.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

from spider_qwen.api.cli import main
from spider_qwen.mcp import handlers


def _run(capsys, argv: list[str]) -> dict:
    rc = main(argv)
    out = capsys.readouterr().out
    assert rc == 0, out
    return json.loads(out)


# --- handlers (always run, offline) ---------------------------------------

def test_procurement_classify_handler():
    res = handlers.procurement_classify("office cleaning Singapore")
    assert res.mode == "service_quote_required"
    assert 0.0 <= res.confidence <= 1.0


def test_evidence_show_and_verify_handlers(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("SPIDER_QWEN_STATE_DIR", str(tmp_path))
    run_id = _run(capsys, ["run", "office cleaning Singapore", "--offline"])["run_id"]

    show = handlers.evidence_show(run_id, state_dir=str(tmp_path))
    assert show.run_id == run_id and show.count > 0 and show.items
    assert all(it.ledger_id and it.url for it in show.items)

    verify = handlers.evidence_verify(run_id, state_dir=str(tmp_path))
    assert verify.checked_claims >= 1
    assert verify.ok is True and verify.chain_ok is True
    assert verify.issues == []


def test_memory_recall_and_reflect_handlers(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("SPIDER_QWEN_STATE_DIR", str(tmp_path))
    query = "example vendor cleaning Singapore quote"
    _run(capsys, ["run", query, "--offline"])

    rec = handlers.memory_recall(query, top_k=5, state_dir=str(tmp_path))
    assert rec.count >= 1 and rec.facts
    assert all(f.entity_name and f.field for f in rec.facts)

    refl = handlers.memory_reflect(state_dir=str(tmp_path))
    assert isinstance(refl.insights, list)


def test_evidence_show_unknown_run_is_empty_not_error(tmp_path):
    show = handlers.evidence_show("run_does_not_exist", state_dir=str(tmp_path))
    assert show.count == 0 and show.items == []


# --- FastMCP adapter (optional dependency) --------------------------------

def test_mcp_server_builds_when_sdk_present():
    pytest.importorskip("mcp")
    from spider_qwen.mcp.server import build_server

    assert build_server() is not None


def test_mcp_server_missing_dependency_has_actionable_error(monkeypatch):
    # Simulate the SDK being absent even if it is installed: None the imported
    # submodule (and parents) so `from mcp.server.fastmcp import FastMCP` raises.
    for mod in ("mcp", "mcp.server", "mcp.server.fastmcp"):
        monkeypatch.setitem(sys.modules, mod, None)
    from spider_qwen.mcp.server import build_server

    with pytest.raises(ImportError) as exc:
        build_server()
    assert "[mcp]" in str(exc.value)


# --- MCP client half (T-7.1b): consume an MCP server as a search backend ---

_FIXTURE_SERVER = os.path.join(os.path.dirname(__file__), "fixtures", "mcp_search_server.py")


def test_mcp_search_backend_from_env_unset_is_none(monkeypatch):
    from spider_qwen.mcp.client import McpSearchBackend

    monkeypatch.delenv("SPIDER_QWEN_MCP_SEARCH_COMMAND", raising=False)
    assert McpSearchBackend.from_env() is None


def test_mcp_search_backend_from_env_parses_command_and_tool(monkeypatch):
    from spider_qwen.mcp.client import McpSearchBackend

    monkeypatch.setenv("SPIDER_QWEN_MCP_SEARCH_COMMAND", "python -m some.search.server")
    monkeypatch.setenv("SPIDER_QWEN_MCP_SEARCH_TOOL", "vendor_search")
    backend = McpSearchBackend.from_env()
    assert backend is not None
    assert backend.command == ["python", "-m", "some.search.server"]
    assert backend.tool == "vendor_search"


def test_unconfigured_qwen_mcp_provider_error_is_actionable(monkeypatch):
    from spider_qwen.tools.search_service import SearchProviderError, build_search_provider

    monkeypatch.delenv("SPIDER_QWEN_MCP_SEARCH_COMMAND", raising=False)
    provider = build_search_provider("qwen_mcp")
    with pytest.raises(SearchProviderError, match="SPIDER_QWEN_MCP_SEARCH_COMMAND"):
        asyncio.run(provider.search("office cleaning Singapore", "Singapore", "en", 3))


def test_mcp_search_backend_round_trip_against_fixture_server():
    pytest.importorskip("mcp")
    from spider_qwen.mcp.client import McpSearchBackend

    backend = McpSearchBackend([sys.executable, _FIXTURE_SERVER])
    rs = asyncio.run(backend("office cleaning Singapore", "Singapore", "en", 2))
    assert rs.provider == "qwen_mcp"
    assert len(rs.results) == 2
    assert all(r.source_tool == "mcp_search" for r in rs.results)
    assert all(r.url.startswith("https://mcp-vendor-") for r in rs.results)


def test_mcp_search_results_are_ledger_backed(monkeypatch):
    pytest.importorskip("mcp")
    from spider_qwen.evidence.ledger import EvidenceLedger
    from spider_qwen.mcp.client import McpSearchBackend
    from spider_qwen.tools.search_service import QwenMcpSearchProvider, SearchService

    backend = McpSearchBackend([sys.executable, _FIXTURE_SERVER])
    ledger = EvidenceLedger("run_mcp_client_test")
    service = SearchService(QwenMcpSearchProvider(backend=backend), ledger)
    rs = asyncio.run(service.search("office cleaning Singapore", limit=2))
    assert rs.results and all(r.evidence_ref is not None for r in rs.results)
    assert {it.source_tool for it in ledger.items()} == {"mcp_search"}


def test_mcp_stdio_server_completes_initialize_list_and_call(tmp_path):
    pytest.importorskip("mcp")
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def _run():
        env = dict(os.environ)
        env["SPIDER_QWEN_STATE_DIR"] = str(tmp_path)
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "spider_qwen.mcp.server"],
            env=env,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                assert "procurement_classify" in names
                result = await session.call_tool(
                    "procurement_classify",
                    {"query": "office cleaning Singapore"},
                )
                assert not getattr(result, "isError", False)
                assert result.content or getattr(result, "structuredContent", None)

    asyncio.run(_run())
