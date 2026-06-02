# Build Log — spider-qwen (SPIDER_QWEN_BUILD_PLAN execution)

One line per completed task. TDD: failing test(s) first, then implementation, then
the guardrail gate (offline e2e + full suite green) before commit.

| Task | Status | Notes |
|---|---|---|
| T-0 | DONE | `models:` block in policy_config.yaml (planner/extraction/embeddings/ocr) + `Policy.model_for(role)` with `SPIDER_QWEN_MODEL_<ROLE>` env overrides; legacy `qwen.*_model` keys and `QWEN_*` env still honoured. 9 new tests (test_models_config.py). Offline run green; 75 tests pass. |
| T-1.1 | DONE | `ranking/serendipity.py`: 4-slot `SerendipityResult` (primary_answer, s1_substitutes, s2_long_tail_sources, s3_risk_signals, evidence_refs, serendipity_score). Deterministic relevance x novelty x unexpectedness scoring vs top-3 baseline; `RiskSignal` model + `extra_risk_signals` seam for later phases. Wired into `RunResult.serendipity` via controller. 10 new tests; 85 pass. |
| T-1.2 | DONE | `serendipity/query_rewrite.py` `expand_query()` -> Step-Back + HyDE + Query2Doc + obsolescence-vocab + MPN-pattern + broker-operator variants (deterministic/offline; optional Qwen HyDE). `Planner.expand_query` passthrough. Golden test on "16-pin SMT op-amp TI..." passes. 6 new tests; 91 pass. |
| T-1.3 | DONE | `serendipity/corrective.py` CRAG `evaluate_retrieval()` (content-coverage + procurement-signal heuristic -> correct/ambiguous/incorrect) + page condensation; `corrective_queries()` (broker-first). Controller refactored to `_gather_queries`; on `incorrect` verdict it broaden/broker-pivots instead of answering. Tracer gains `detail`; metrics expose crag_verdict/confidence/corrective_searches. 7 new tests; 98 pass. |
| T-1.4 | DONE | `agent/compiler.py`: `TokenBucket` + `RateLimiter` (5 search/min, 25 fetch/min, injectable clock) + `LLMCompiler` DAG executor (level-parallel `asyncio.gather`, cycle/unknown-dep checks, per-kind peak-concurrency + DAG trace). Controller gains `rate_limiter`/`compiler` and `gather_parallel` (used by T-3.3) on a shared `_fetch_and_extract`. 8 new tests; 106 pass. **Phase 1 checkpoint:** offline hero query runs end-to-end (serendipity slots + CRAG verdict). |
