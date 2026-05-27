# Memory Design

The memory layer is closed-loop enough for the MemoryAgent story: facts are
written, decayed, recalled under a context budget, and revalidated.

## Working memory (per run)
`run_id, query, mode, candidate_urls, fetched_pages, extracted_candidates`.
Discarded after the run.

## Episodic memory (one summary per run)
Appended to `<state_dir>/memory/episodic.jsonl`:
`episode_id, query, mode, summary, created_at, evidence_refs, outcome`
(`outcome ∈ {success, incomplete, failed}`).

## Semantic memory (evidence-backed facts)
`<state_dir>/memory/semantic.json`:
`fact_id, entity_type, entity_name, field, value, confidence, privacy_class,
evidence_refs, created_at, last_verified_at, status` (`active | stale | disputed`).

**Promotion requires evidence** (`memory/promotion.py`): 2+ independent sources,
or 1 high-confidence source + domain-ownership signal.

**Conflict policy** (`SemanticMemory.upsert`): same value → boost confidence +
merge refs; clearly higher-confidence new value → replace; otherwise → `disputed`.
**Disputed facts are excluded from RFQ enrichment in code.**

## Decay (`memory/decay.py`)
`apply_decay` = `confidence * 0.5 ** (age_days / half_life)` (half-life 90d).
`is_stale` flags facts past the stale threshold. `SemanticMemory.maintain()`
applies that policy and persists `status = stale`.

## Recall

`SemanticMemory.recall(query, top_k=5, context_budget_chars=1200)` returns active
facts only. Ranking uses query-term overlap and decayed confidence; returned
facts must fit the context budget. The controller invokes this through the
semantic-memory MCP seam and can re-record a recalled quote channel into the
current run ledger as `semantic_memory` evidence.

## Revalidation (`memory/revalidation.py`)
`spider-qwen memory revalidate <fact_id>` refreshes one fact against new
evidence; contradiction → `disputed`, no fresh evidence → `stale`.

## CLI

```bash
spider-qwen memory show
spider-qwen memory revalidate <fact_id> --value sales@example.sg --confidence 0.9
```
