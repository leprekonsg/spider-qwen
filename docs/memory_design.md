# Memory Design (v1)

Kept simple. (`memory/`) Deferred: procedural memory, autonomous playbooks,
complex decay analytics, automated revalidation scheduling.

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
**Disputed facts must not be used in RFQ drafts.**

## Decay (`memory/decay.py`)
`apply_decay` = `confidence * 0.5 ** (age_days / half_life)` (half-life 90d).
`is_stale` flags facts past 180d since `last_verified_at`.

## Revalidation (`memory/revalidation.py`)
Manual in v1: refresh one fact against new evidence; contradiction → `disputed`,
no fresh evidence → `stale`.
