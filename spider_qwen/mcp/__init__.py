"""T-7.1: Model Context Protocol surface for spider-qwen.

``handlers`` holds pure, deterministic, dependency-free functions backing each
exposed tool (always importable + tested). ``server`` is a thin FastMCP adapter
that needs the optional ``mcp`` SDK (``pip install -e '.[mcp]'``). ``schemas``
holds the typed input/output models.

v1 exposes read-only, bounded tools only (classify / evidence / memory). Tools
that mutate files, send RFQs, browse Drive, or call DashScope are intentionally
excluded; live third-party MCP consumption is deferred to Phase 8.
"""
