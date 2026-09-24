# Evidence Model

All important outputs are evidence-backed. (`evidence/`)

## EvidenceRef (embedded in outputs)

```json
{ "ledger_id": "ev_...", "url": "...", "snippet_hash": "<sha256>", "retrieved_at": "<ISO-8601>" }
```

## EvidenceItem (full ledger record)

`schema_version, ledger_id, source_tool, url, final_url, title, retrieved_at,
snippet, snippet_hash, text, text_hash, language, metadata, confidence,
reliability, parent_hash, chain_hash`.

`source_tool ∈ {tinyfish_search, tinyfish_fetch, qwen_web_extractor, wayback_cdx,
legacy_ocr, mcp_search, semantic_memory, mock}`. `qwen_web_extractor` page text
is written by a model and never grounds a claim.

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

An extraction row is recorded only when its value is located in the parent
page: in the text (offsets + `span_hash`) or among the page's recorded outbound
links (`"located_in": "links"`, no offsets). A value found in neither gets no
row, so it cannot cite itself.

`spider-qwen evidence verify <run_id>` reloads the ledger and checks every row
with a `parent_ledger_id`: text-located rows by slicing the parent text and
checking the span and snippet hashes, link-located rows against the parent's
recorded links. A parent-linked row located in neither is an issue.

## Rules (enforced)

- No ranking output without `evidence_refs` — rankers drop candidates whose
  ref list is empty (they do not judge support; the verification spine does).
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

`EvidenceLedger.persist()` writes `<state_dir>/evidence/<run_id>.ledger.json`
(with a top-level `schema_version`) via a fsynced temp file and an atomic
replace. `EvidenceLedger.load(run_id, state_dir)` rehydrates it and refuses a
file whose rows no longer match their hash chain or published `tree_head`;
`evidence verify` loads without that check so it can list every altered row.
The CLI exposes it via `spider-qwen evidence show <run_id>`, `evidence verify`,
and `evidence graph`.

Crash safety: when a state dir is configured, every `record()` also appends the
row to `<run_id>.ledger.wal.jsonl` and fsyncs it. A successful `persist()`
removes the WAL (the canonical file supersedes it); if the run crashed before
persisting, or the canonical file is unreadable, `load()` replays the WAL
instead, dropping a torn final line. WAL recovery is
the pre-verification record: verifier annotations written after `record()` live
only in the canonical file, and no `tree_head` was published for a crashed run,
so the chain is resealed from row content on replay.

## Transparency proofs

Persisted ledgers include a Merkle `tree_head`. When
`SPIDER_QWEN_STH_SIGNING_KEY` is configured, the ledger also stores a signed tree
head. `evidence prove` emits a citation proof with that signed head embedded.
External verification must pin the Ed25519 public key out of band via
`--sth-public-key`, `SPIDER_QWEN_STH_PUBLIC_KEY`, or
`SPIDER_QWEN_STH_PUBLIC_KEY_FILE`; the public key embedded in the signed head is
informational, not a trust anchor. Pass `--require-sth` to `evidence verify` or
`evidence prove` for external-verifier mode: the command then fails unless the
signed tree head verifies against a configured anchor (a missing anchor is a
failure, not a skip).
