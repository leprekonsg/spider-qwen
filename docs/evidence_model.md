# Evidence Model

All important outputs are evidence-backed. (`evidence/`)

## EvidenceRef (embedded in outputs)

```json
{ "ledger_id": "ev_...", "url": "...", "snippet_hash": "<sha256>", "retrieved_at": "<ISO-8601>" }
```

## EvidenceItem (full ledger record)

`schema_version, ledger_id, source_tool, url, final_url, title, retrieved_at,
snippet, snippet_hash, text, text_hash, language, metadata, confidence`.

`source_tool ∈ {tinyfish_search, tinyfish_fetch, qwen_web_extractor, mcp_search,
semantic_memory, mock}`.

## Claim/span metadata

Extraction evidence keeps `EvidenceRef` backward compatible and stores claim
details in `EvidenceItem.metadata`:

```json
{
  "claim_id": "claim_...",
  "field": "quote_channel",
  "parent_ledger_id": "ev_...",
  "start_char": 120,
  "end_char": 141,
  "span_hash": "<sha256>"
}
```

`spider-qwen evidence verify <run_id>` reloads the ledger, slices the parent page
text, and checks the span hash plus snippet hash. Link-only evidence remains
ledger-backed but may not have character offsets.

## Rules (enforced)

- No ranking output without `evidence_refs` — rankers drop evidence-less candidates.
- No `RFQDraft` without `quote_channel.evidence_ref`.
- No semantic-memory promotion without `evidence_ref`.
- Downstream evidence is referenced by `ledger_id`, never as a free-text URL.

## Dedupe (`evidence/dedupe.py`)

- exact `text_hash` dedupe
- `snippet_hash` dedupe
- URL canonicalization (scheme normalized, `www.`/trailing slash stripped,
  tracking params removed)
- near-duplicate snippet similarity (`difflib`, threshold 0.92)

## Persistence

`EvidenceLedger.persist()` writes `<state_dir>/evidence/<run_id>.ledger.json`.
`EvidenceLedger.load(run_id, state_dir)` rehydrates it; the CLI exposes it via
`spider-qwen evidence show <run_id>`, `evidence verify`, and `evidence graph`.
