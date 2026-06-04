# Policy Config

`spider_qwen/governance/policy_config.yaml`, loaded by `agent/policy.py`.
Controls budgets, geo defaults, privacy tags, Qwen optional paths, RFQ behavior,
memory rules, and HITL review.

## Budgets (per mode)

Each mode obeys the stop tuple
`(max_tool_calls, min_validated_candidates, evidence_completeness_threshold)`.

| Mode | search | fetch URLs | min validated | evidence threshold |
|---|--:|--:|--:|--:|
| product_exact_price | 4 | 20 | 5 | 0.75 |
| service_quote_required | 5 | 25 | 3 | 0.65 |
| contact_enrichment_only | 3 | 15 | 1 | 0.70 |

`BudgetTracker` blocks calls past a limit and records a `StopReason`
(`max_search_calls_reached`, `max_fetch_urls_reached`, `max_runtime_reached`,
`insufficient_evidence`, `no_quote_channel_found`, `min_validated_candidates_met`, …).

## Geo
`default_region: SEA`, `fallback_region: global`, plus `boost_countries`.

## Privacy
`review_gate_enabled` per privacy class. `named_person_high_sensitivity` is ON
by default; `business_contact` stays OFF.
`high_sensitivity_fields`: `named_person_email`, `named_person_phone`,
`direct_mobile`.

## Models (role -> Qwen model)

Canonical model role assignments live under `models:`; never hard-code model
strings in code. Resolve with `Policy.model_for(role)`.

| Role | Default | Use |
|---|---|---|
| `planner` | `qwen3.7-max` | planning / reasoning / high-stakes judging |
| `extraction` | `qwen3.5-flash` | extraction / classification / query-variants |
| `extraction_fallback` | `qwen-flash` | flash snapshot unavailable |
| `embeddings` | `text-embedding-v4` | skills / notes / FFF specs |
| `ocr` | `qwen-vl-ocr-2025-11-20` | datasheet / legacy-book OCR |

Override a role at runtime with env `SPIDER_QWEN_MODEL_<ROLE>` (e.g.
`SPIDER_QWEN_MODEL_PLANNER`). Pin dated snapshots for demo reproducibility.

## Qwen

- `structured_extraction_enabled`
- `router_fallback_enabled`
- `router_confidence_threshold`
- `page_judge_enabled` — gate fetched pages (relevance/freshness/authority/contradicts-ledger) before persist; reject/flag low-trust pages. Env `QWEN_PAGE_JUDGE_ENABLED`.

Legacy keys `router_model` / `json_extractor_model` are still honoured if present
(they override `models.planner` / `models.extraction`). New configs should use
the `models:` block. Env `QWEN_ROUTER_MODEL` / `QWEN_JSON_EXTRACTOR_MODEL` and
`SPIDER_QWEN_MODEL_*` override config for live demos.

## Verification

- `verification.enabled` — T-2.2 verification spine. Decomposes each candidate
  into atomic claims, checks every `(claim, evidence_span)` with a MiniCheck-style
  entailment gatekeeper (grounding the claim value against the source *page* text,
  not the extraction snippet), and re-grounds flagged atoms against the wider
  evidence corpus (SAFE). Candidates whose critical claims are not grounded are
  blocked from output; `verified` / `verifier_score` are written onto each claim
  ledger row. Heuristic + offline by default; off so the base pipeline is
  unchanged. Env `SPIDER_QWEN_VERIFICATION_ENABLED`. Learned NLI (MiniCheck-FT5)
  and live search (TinyFish) plug into the `MiniCheck(model=...)` /
  `SafeReverifier(search_fn=...)` seams.

## RFQ
`allow_vendor_submission: false` (v1 hard rule — never overridden in code),
`default_tone`, `minimum_checklist_completeness: 0.65`.

## Memory
`semantic_promotion_requires_evidence: true`,
`allow_disputed_facts_in_rfq: false`.

## HITL

`hitl.enabled: true` persists pending review events. `hitl.require_review: false`
keeps default CLI runs non-blocking.

## Overriding
Pass a path to `load_policy("/path/to/policy_config.yaml")`, or construct
`Controller(policy=...)`. Tune budgets/thresholds there — not in code.
