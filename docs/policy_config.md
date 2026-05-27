# Policy Config

`spider_qwen/governance/policy_config.yaml`, loaded by `agent/policy.py`.
Controls budgets, geo defaults, privacy tags, RFQ behavior, and memory rules.

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
`review_gate_enabled` per privacy class (default **false** in v1).
`high_sensitivity_fields`: `named_person_email`, `named_person_phone`,
`direct_mobile`. Named-person data is tagged high-sensitivity but not gated by
default; gates are configurable for later phases.

## RFQ
`allow_vendor_submission: false` (v1 hard rule — never overridden in code),
`default_tone`, `minimum_checklist_completeness: 0.65`.

## Memory
`semantic_promotion_requires_evidence: true`,
`allow_disputed_facts_in_rfq: false`.

## Overriding
Pass a path to `load_policy("/path/to/policy_config.yaml")`, or construct
`Controller(policy=...)`. Tune budgets/thresholds there — not in code.
