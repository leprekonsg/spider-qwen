# spider-qwen

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="spider-qwen-logo-darkmode.png">
    <img alt="Spider-Qwen logo" src="spider-qwen-logo-transparent.png" width="720">
  </picture>
</p>

Deterministic, evidence-first **procurement research agent**. Give it a query
like *"Find office cleaning vendors in Singapore and prepare RFQ drafts"* and it
returns an evidence-backed vendor shortlist plus ready-to-review RFQ drafts —
without ever submitting or sending anything.

- **Controller:** Qwen (planner/controller in spirit; v1 execution is deterministic and policy-bound)
- **Web layer:** [TinyFish](https://docs.tinyfish.ai) Search + Fetch (primary), Qwen WebExtractor (single-page fallback)
- **Guarantee:** every vendor / contact / price / RFQ output references a ledger entry. No evidence, no output.

> v1 scope: `search → fetch → extract → rank → RFQ draft → persist evidence`.
> **No** portal submission, browser automation, or code interpreter.

## Why "evidence-first"

Procurement decisions need provenance. Every search result, fetched page, and
extracted fact is written to an append-only **evidence ledger** with a SHA-256
snippet hash and timestamp. Downstream outputs carry lightweight `EvidenceRef`
pointers (`ledger_id`) back into that ledger — never bare URLs. A ranked
candidate with no evidence is dropped, not scored.

## Install

Requires **Python 3.11+**.

```bash
git clone <repo> && cd spider-qwen
pip install -e ".[dev]"           # core + test deps
# optional extras:
pip install -e ".[qwen]"          # Qwen WebExtractor (openai SDK)
pip install -e ".[server]"        # FastAPI HTTP server
```

Copy `.env.example` to `.env` and add your keys (`.env` is gitignored — never commit it).

## Quickstart

No API keys needed — `--offline` uses deterministic mock providers:

```bash
spider-qwen classify "office cleaning Singapore"
spider-qwen run "office cleaning Singapore" --offline
spider-qwen run "500 ergonomic chairs Singapore" --mode product_exact_price --offline
spider-qwen evidence show <run_id>
spider-qwen benchmark --gold-set spider_qwen/benchmarks/gold_set.json
```

Example output (trimmed):

```json
{
  "run_id": "run_abc123",
  "mode": "service_quote_required",
  "stop_reason": "min_validated_candidates_met",
  "validated_candidates": [
    {
      "vendor_name": "Example Cleaning Pte Ltd",
      "website": "https://example-cleaning.sg",
      "pricing_status": "QUOTE_REQUIRED",
      "quote_channel": { "type": "contact_email", "value": "sales@example-cleaning.sg",
        "evidence_ref": { "ledger_id": "ev_001", "url": "...", "snippet_hash": "...", "retrieved_at": "..." } },
      "score": 82.5
    }
  ],
  "rfq_drafts": [ { "status": "complete", "rfq_email_template": "Dear ...", "quote_channel": {"...": "..."} } ],
  "evidence_refs": [ "..." ]
}
```

## Architecture

```
User Query / Email
        ↓
Mode Classifier           deterministic, keyword/intent scored
        ↓
Budgeted Controller       fixed per-mode plan; obeys budgets + policy
        ↓
Search Service  →  Fetch Service          TinyFish (provider-abstracted)
        ↓
Extraction Modules        pricing · quote_channel · contact · service_match · vendor
        ↓
Evidence Ledger           every source recorded; deduped
        ↓
Ranking                   SEA-first geo boost + per-mode weights
        ↓
RFQ Draft / Supplier Result
        ↓
Working + Episodic + Semantic Memory
```

## Procurement modes

| Mode | When | Output |
|---|---|---|
| `product_exact_price` | products with expected public pricing | priced candidates with `PricingStatus` + evidence |
| `service_quote_required` | services where price is quote-only | vendor shortlist + quote channel + **RFQ draft** |
| `contact_enrichment_only` | you have vendors, need contacts | evidence-backed contacts + validation signals |
| `revalidation` | refresh a stale memory fact | refreshed/`stale`/`disputed` fact (manual in v1) |

### Pricing ontology

`EXACT_PRICE · PRICE_RANGE · STARTING_FROM · RATE_CARD_FOUND · QUOTE_REQUIRED ·
CONTACT_FOR_PRICING · NOT_FOUND · CONFLICTING`. A missing price is **never** a
global failure — it becomes `NOT_FOUND`/`CONTACT_FOR_PRICING` and only blocks
`product_exact_price`, which requires a price by contract.

## Providers (swappable)

Selected via env or injection; both abstracted behind protocols.

| Env | Values | Default |
|---|---|---|
| `SPIDER_QWEN_SEARCH_PROVIDER` | `tinyfish` · `qwen_mcp` · `mock` | `tinyfish` |
| `SPIDER_QWEN_FETCH_PROVIDER` | `tinyfish` · `qwen_web_extractor` · `mock` | `tinyfish` |

Qwen Code has no built-in web search (it is MCP-based), so search is a provider
interface, not a hard-coded service. `QwenMcpSearchProvider` is the seam: inject
an MCP/responses backend, or fall back to TinyFish.

## RFQ drafts — and what spider-qwen never does

RFQ drafts contain exactly: `rfq_email_template`, `required_inputs_checklist`,
`quote_channel` (with `evidence_ref`), and `assumptions_and_limits`. Hard stops:

- No evidenced quote channel → no polished RFQ (status `incomplete`).
- Checklist completeness below threshold (default `0.65`) → status `incomplete`.

The agent **never** submits forms, sends email, drives a browser, or runs a code
interpreter. The audit log refuses to record any such action.

## Configuration & governance

`spider_qwen/governance/policy_config.yaml` controls budgets (per mode), geo
defaults, privacy tags, RFQ behavior, and memory rules. Budgets enforce the stop
tuple `(max_tool_calls, min_validated_candidates, evidence_completeness_threshold)`.
Named-person contacts are tagged high-sensitivity; review gates are configurable
and disabled by default in v1.

## Project layout

```
spider_qwen/
  agent/         controller, budget, planner, policy, tool_registry, execution_context
  modes/         classifier, contracts (enums + candidate schemas), router
  tools/         tinyfish_client, search_service, fetch_service, qwen_web_extractor, provider_types
  extraction/    pricing, quote_channel, contact, vendor_metadata, service_match, dedupe
  ranking/       product/service/contact rankers, geo_strategy
  evidence/      models, ledger, dedupe, bundles
  memory/        working, episodic, semantic, decay, promotion, revalidation
  rfq/           schema, checklist, generator
  governance/    policy_config.yaml, privacy, review, audit
  observability/ metrics, tracing
  api/           schema, cli, server
  benchmarks/    gold_set.json, evaluators, baseline comparison
docs/            architecture & design references
tests/           unit + end-to-end suite
```

## Testing

```bash
python -m pytest -q
```

Covers modes, pricing ontology, quote-channel detection, evidence ledger,
rankers, RFQ hard-stops, budget tracker, and **end-to-end** runs through the CLI
(persisted evidence ledger, traces, audit log, and the benchmark harness).

## Roadmap (5 phases)

0. Foundation · 1. Product + contact parity · 2. Service quote + RFQ · 3. Memory
MVP + revalidation · 4. Governance hardening + advanced workflows (browser/agent
and code interpreter remain out of v1).

## License

MIT — see [LICENSE](LICENSE).
