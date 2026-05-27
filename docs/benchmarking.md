# Benchmarking

`spider_qwen/benchmarks/`. Run via:

```bash
spider-qwen benchmark --gold-set spider_qwen/benchmarks/gold_set.json
```

Add `--live` to use live providers instead of the deterministic mock.

## Sources

1. **Gold set** — curated cases per mode (`gold_set.json`).
2. **Baseline parity** — compare discovered vendor domains to an external
   baseline (e.g. b2b-scrape) via `compare_to_baseline.py`. No-op if the
   baseline file is absent.
3. **Synthetic edge cases** — conflict, missing price, no quote channel,
   duplicate evidence (covered in the unit suite).

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

Offline note: the mock fetcher fabricates no prices, so `product_exact_price`
cases validate nothing offline by design — run `--live` to benchmark pricing.
