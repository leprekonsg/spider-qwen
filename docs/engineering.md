# Engineering Notes

## Non-obvious decisions

- **Evidence as control surface:** candidates, contacts, prices, memory facts,
  and RFQ drafts carry `EvidenceRef`; evidence-less candidates are not ranked.
- **Append-only ledger:** each run persists a JSON ledger with SHA-256 snippet,
  text, and span hashes.
- **Deterministic default:** regex extractors and mock providers remain the hot
  path for repeatable demos and tests.
- **Optional Qwen paths:** Qwen JSON extraction and tool-call routing are opt-in,
  schema-validated, and fail back to deterministic behavior.
- **Hard RFQ boundary:** audit refuses submit/send/form actions with
  `PolicyViolation`.
- **Stop tuple:** budgets combine max search/fetch/extraction calls,
  `min_validated_candidates`, and `evidence_completeness_threshold`.

## Qwen integration

- `QwenJsonExtractor`: fetched page text → schema-constrained procurement JSON.
- `QwenModeRouter`: low-confidence classifier fallback via tool calling.
- Qwen Code skills in `.qwen/skills/*` are loaded into Qwen extraction prompts.
- `SemanticMemoryMcpAdapter` exposes MCP-shaped recall and revalidate tools.

## Reliability posture

Live provider failures, malformed Qwen JSON, unavailable API keys, and missing
optional dependencies do not block deterministic extraction. Those paths emit
trace errors and continue with local extractors.
