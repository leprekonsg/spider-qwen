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

- `mode_classification_accuracy`
- `quote_channel_precision`, `rfq_draft_completeness` (service)
- `pricing_status_accuracy` (product — `evaluate_product_mode.py`)
- `evidence_coverage` — of runs that validated a candidate, the fraction whose
  candidates are evidence-backed (the core invariant; should be 1.0)
- `runtime_seconds`
- `per_mode` — case count and mode accuracy per procurement mode.

Offline note: the mock fetcher is deterministic and now includes product pricing,
missing-price, conflicting-price, and rate-card cases. Report offline and live
numbers separately; never present fixture-backed scores as live-web validity.
Mode classification accuracy is a deterministic classifier regression unless it
comes from `live_validation_set.json`.
