# Benchmarking

`spider_qwen/benchmarks/`. Run via:

```bash
spider-qwen benchmark --gold-set spider_qwen/benchmarks/gold_set.json
```

Add `--live` to use live providers instead of the deterministic mock.

## Sources

1. **Gold set** — 100 curated offline cases, 20 per mode (`gold_set.json`).
2. **Baseline parity** — compare discovered vendor domains to an external
   baseline (e.g. b2b-scrape) via `compare_to_baseline.py`. No-op if the
   baseline file is absent.
3. **Synthetic edge cases** — conflict, missing price, no quote channel,
   duplicate evidence, stale memory, and disputed facts.
4. **Live validation set** — `live_validation_set.json`, intended for small
   rate-limited runs with live providers.

## Gold case shape

```json
{
  "case_id": "svc_sg_cleaning_001",
  "query": "office cleaning Singapore",
  "expected_mode": "service_quote_required",
  "expected_pricing_status": ["QUOTE_REQUIRED", "CONTACT_FOR_PRICING"],
  "must_find": { "quote_channel": true, "rfq_draft_status": "complete" }
}
```

## Metrics

- `end_to_end_routing_accuracy` (alias `mode_classification_accuracy`)
- `quote_channel_yield`, `rfq_draft_yield` (service)
- `candidate_evidence_presence_rate` (alias `evidence_coverage`) — of runs that
  emitted candidates, the fraction whose candidates carry evidence refs
- `candidate_evidence_validity_rate` — of those runs, the fraction whose cited
  ledger rows exist and whose ledger passes `verify_ledger` (should be 1.0)
- `must_find` — expectation outcomes. A negative expectation (`false`) is
  `unavailable` when nothing was emitted, not `passed`.
- `adversarial_cases` — how many tagged cases the offline mock actually simulates
- `runtime_seconds`
- `per_mode` — case count, `emitted_run_rate` and routing accuracy per mode.

`pricing_status_accuracy` is always null: an aggregate status is not an
offering label. Offering correctness needs `expected_offerings` records
(`evaluate_product_mode.py`).

Offline note: the mock search provider synthesizes results from the query
string and the mock fetcher synthesizes page text from the URL slug, so offline
quote-channel and RFQ yields are true by construction. Only the `missing_price`,
`conflicting_price` and `rate_card` adversarial tags change mock behaviour; the
rest are labels. Offline scores are a regression check on routing, contracts
and evidence integrity, not a quality measure. Report offline and live numbers
separately; never present fixture-backed scores as live-web validity.
