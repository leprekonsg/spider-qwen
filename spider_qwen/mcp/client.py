"""MCP stdio client: consume an external MCP server's tool as a search backend.

This is the client half of the MCP story (the server half is ``server.py``).
``McpSearchBackend`` plugs into ``QwenMcpSearchProvider`` so any MCP server that
exposes a search-shaped tool can drive discovery, while the evidence ledger
records results as ``source_tool="mcp_search"`` like any other provider.

Requires the optional ``mcp`` SDK (``pip install -e '.[mcp]'``); importing this
module never requires it until a tool call is made. The DashScope Responses-API
``tools=[{type: mcp}]`` wiring (server-side MCP execution) remains deferred.
"""

from __future__ import annotations

import json
import os
import shlex
from typing import Any, Sequence

from ..tools.provider_types import SearchResult, SearchResultSet

_MISSING = (
    "The spider-qwen MCP client needs the optional 'mcp' SDK. "
    "Install it with: pip install -e '.[mcp]'"
)


class McpClientError(Exception):
    pass


async def call_stdio_tool(
    command: str,
    args: Sequence[str],
    tool: str,
    arguments: dict[str, Any],
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Spawn ``command args``, complete the MCP handshake, call one tool, return its payload.

    One session per call: stateless and simple. Spawn cost is acceptable at v1
    call volumes (a few searches per run); revisit if a backend needs a warm
    session. Prefers ``structuredContent``; falls back to parsing the first JSON
    text block.
    """
    try:
        from mcp.client.session import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
    except ImportError as exc:
        raise ImportError(_MISSING) from exc

    merged_env = dict(os.environ)
    if env:
        merged_env.update(env)
    params = StdioServerParameters(command=command, args=list(args), env=merged_env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, arguments)
            if getattr(result, "isError", False):
                raise McpClientError(f"MCP tool {tool!r} returned an error: {result.content}")
            payload = getattr(result, "structuredContent", None)
            if isinstance(payload, dict):
                return payload
            for block in result.content or []:
                text = getattr(block, "text", None)
                if text:
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError as exc:
                        raise McpClientError(
                            f"MCP tool {tool!r} returned non-JSON text content"
                        ) from exc
            raise McpClientError(f"MCP tool {tool!r} returned no parseable content")


class McpSearchBackend:
    """Adapt one MCP server tool into the ``QwenMcpSearchProvider`` backend coroutine.

    The configured tool is called as ``{"query": ..., "limit": ...}`` and must
    return ``{"results": [{"url": ..., "title"?: ..., "snippet"?: ...}, ...]}``.
    Items without a ``url`` are skipped, mirroring the TinyFish provider.
    """

    def __init__(self, command: Sequence[str], tool: str = "web_search") -> None:
        if not command:
            raise ValueError("McpSearchBackend needs a non-empty server command.")
        self.command = list(command)
        self.tool = tool

    @classmethod
    def from_env(cls) -> "McpSearchBackend | None":
        """Build from SPIDER_QWEN_MCP_SEARCH_COMMAND / _TOOL; None when unset."""
        raw = os.getenv("SPIDER_QWEN_MCP_SEARCH_COMMAND", "").strip()
        if not raw:
            return None
        return cls(shlex.split(raw), tool=os.getenv("SPIDER_QWEN_MCP_SEARCH_TOOL", "web_search"))

    async def __call__(
        self, query: str, location: str | None, language: str, limit: int
    ) -> SearchResultSet:
        payload = await call_stdio_tool(
            self.command[0], self.command[1:], self.tool, {"query": query, "limit": limit}
        )
        results: list[SearchResult] = []
        for rank, item in enumerate(payload.get("results", [])[:limit]):
            url = item.get("url") if isinstance(item, dict) else None
            if not url:
                continue
            results.append(
                SearchResult(
                    url=url,
                    title=item.get("title"),
                    snippet=item.get("snippet") or "",
                    rank=rank,
                    source_tool="mcp_search",
                )
            )
        return SearchResultSet(
            query=query,
            location=location,
            results=results,
            total_results=len(results),
            provider="qwen_mcp",
        )
