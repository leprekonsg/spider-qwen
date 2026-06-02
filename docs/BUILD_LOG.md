# Build Log — spider-qwen (SPIDER_QWEN_BUILD_PLAN execution)

One line per completed task. TDD: failing test(s) first, then implementation, then
the guardrail gate (offline e2e + full suite green) before commit.

| Task | Status | Notes |
|---|---|---|
| T-0 | DONE | `models:` block in policy_config.yaml (planner/extraction/embeddings/ocr) + `Policy.model_for(role)` with `SPIDER_QWEN_MODEL_<ROLE>` env overrides; legacy `qwen.*_model` keys and `QWEN_*` env still honoured. 9 new tests (test_models_config.py). Offline run green; 75 tests pass. |
