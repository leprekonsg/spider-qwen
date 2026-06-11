"""Fixture MCP stdio server exposing a canned web_search tool (tests only).

Spawned by tests via ``python tests/fixtures/mcp_search_server.py`` to exercise
the spider-qwen MCP client end to end. Deterministic, no network.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

server = FastMCP("fixture-search")


@server.tool()
def web_search(query: str, limit: int = 10) -> dict:
    """Return deterministic search results for a query."""
    slug = "".join(c if c.isalnum() else "-" for c in query.lower()).strip("-")
    n = max(1, min(limit, 3))
    return {
        "results": [
            {
                "url": f"https://mcp-vendor-{i}.example/{slug}",
                "title": f"MCP vendor {i} - {query}",
                "snippet": f"Provider {i} for {query}. Request a quotation via our contact page.",
            }
            for i in range(1, n + 1)
        ]
    }


if __name__ == "__main__":
    server.run()
