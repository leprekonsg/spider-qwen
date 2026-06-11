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
