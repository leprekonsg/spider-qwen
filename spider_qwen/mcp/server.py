"""T-7.1: FastMCP stdio server exposing spider-qwen's read-only tools.

Requires the optional ``mcp`` SDK: ``pip install -e '.[mcp]'``. The handlers in
``handlers.py`` are dependency-free and always tested; this module only wires them
into a FastMCP server, so importing it never requires ``mcp`` until ``build_server``
is called. Run as a stdio server with ``python -m spider_qwen.mcp.server``.

Exposes only read-only, bounded tools. Live third-party MCP consumption (Google
Drive for RFP docs) and the DashScope Responses-API ``tools=[{type:mcp}]`` wiring
are deferred to Phase 8.
"""

from __future__ import annotations

from . import handlers

_MISSING = (
    "The spider-qwen MCP server needs the optional 'mcp' SDK. "
    "Install it with: pip install -e '.[mcp]'"
)


def build_server():
    """Construct the FastMCP server, registering the read-only tools.

    Raises ImportError with an actionable message if the optional SDK is absent.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # SDK not installed (or shadowed)
        raise ImportError(_MISSING) from exc

    server = FastMCP("spider-qwen")

    @server.tool()
    def procurement_classify(query: str) -> dict:
        """Classify a procurement query into a mode."""
        return handlers.procurement_classify(query).model_dump(mode="json")

    @server.tool()
    def evidence_show(run_id: str) -> dict:
        """List the evidence ledger items recorded for a run."""
        return handlers.evidence_show(run_id).model_dump(mode="json")

    @server.tool()
    def evidence_verify(run_id: str) -> dict:
        """Re-verify a run's evidence spans and Merkle hash chain."""
        return handlers.evidence_verify(run_id).model_dump(mode="json")

    @server.tool()
    def memory_recall(query: str, top_k: int = 5) -> dict:
        """Recall evidence-backed semantic facts relevant to a query."""
        return handlers.memory_recall(query, top_k=top_k).model_dump(mode="json")

    @server.tool()
    def memory_reflect() -> dict:
        """Distil reflections over the learned facts and run episodes."""
        return handlers.memory_reflect().model_dump(mode="json")

    return server


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
