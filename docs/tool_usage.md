# Tool Usage

Start with the lightest tool and escalate only when needed:
`search → fetch → deterministic extraction → optional Qwen JSON`. Browser,
submission, code interpreter, and non-Qwen LLMs are out of scope.

## TinyFish (primary)

`tools/tinyfish_client.py` (httpx, async).

- **Search** — `GET https://api.search.tinyfish.ai` · params `query, location, language, page` → ranked URL results.
- **Fetch** — `POST https://api.fetch.tinyfish.ai` · body `urls (≤10), format, links, image_links` → clean per-URL content + links + per-URL errors.
- Auth: `X-API-Key` header. Retries on 429/5xx with backoff.

`search_service.py` / `fetch_service.py` wrap the client, record every result to
the evidence ledger, and enforce the budget.

## Provider abstraction

`SearchProvider` / `FetchProvider` protocols (`tools/provider_types.py`):

| Search | Fetch |
|---|---|
| `TinyFishSearchProvider` | `TinyFishFetchProvider` |
| `QwenMcpSearchProvider` (MCP stdio backend) | `QwenWebExtractorFetchProvider` |
| `MockSearchProvider` | `MockFetchProvider` |

Select via `SPIDER_QWEN_SEARCH_PROVIDER` / `SPIDER_QWEN_FETCH_PROVIDER`. The
`qwen_mcp` provider spawns the MCP stdio server named by
`SPIDER_QWEN_MCP_SEARCH_COMMAND` and calls `SPIDER_QWEN_MCP_SEARCH_TOOL`
(default `web_search`); results are ledger-backed as `source_tool="mcp_search"`.

## Qwen WebExtractor (single-page fallback)

`tools/qwen_web_extractor.py` — Alibaba Model Studio responses API with the
`web_extractor` tool (`Authorization: Bearer $DASHSCOPE_API_KEY`, OpenAI-compatible
SDK). Use only when TinyFish Fetch is unavailable, for single-URL Qwen-native
extraction, or for benchmark comparison. Requires the `qwen` extra.

## Qwen JSON extractor

`tools/qwen_json_extractor.py` takes already-fetched page text and asks Qwen for
schema-constrained procurement JSON. It is opt-in via
`QWEN_STRUCTURED_EXTRACTION_ENABLED=1` or `--qwen-json`; `--offline --qwen-json`
uses `MockQwenJsonExtractor`.

## Qwen router

`modes/qwen_router.py` is called only when deterministic classification
confidence is below `QWEN_ROUTER_CONFIDENCE_THRESHOLD` and
`QWEN_ROUTER_FALLBACK_ENABLED=1`.

## MCP memory seam

`memory/mcp.py` exposes `semantic_memory.recall` and
`semantic_memory.revalidate` through an MCP-shaped adapter. Controller recall
uses this seam before ranking.

## Not in scope

TinyFish Agent / Browser, Qwen code interpreter, form submission, and email
sending. `ToolRegistry` allowlist remains narrow.
