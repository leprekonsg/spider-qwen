# Architecture

```
User Query → Mode Classifier → Budgeted Controller
  → Search Service → Fetch Service
  → Extraction → Evidence Ledger → Ranking
  → RFQ Draft / Result → Working + Episodic + Semantic Memory
```

## Control vs. execution

Qwen is the planner/controller in spirit. In v1 the plan is a **fixed,
deterministic pipeline per mode** (`agent/planner.py`); the controller
(`agent/controller.py`) executes it under hard budgets (`agent/budget.py`) and
policy (`agent/policy.py`). Swapping in an LLM planner later requires no change
to the controller's tool calls.

## Request lifecycle

1. **Classify** — `modes/classifier.py` scores intent terms → `ProcurementMode`.
2. **Route + budget** — `modes/router.py` maps mode → extractors/ranker; policy
   supplies the `Budget`.
3. **Gather (SEA-first)** — build geo query templates, `search` within budget,
   `fetch` candidate URLs, run extractors, build candidates with `EvidenceRef`s.
4. **Rank + validate** — per-mode ranker scores; `_is_validated` applies the
   mode contract + `evidence_completeness_threshold`.
5. **Global fallback** — if validated < `min_validated_candidates` and budget
   remains, re-gather with global templates.
6. **RFQ** (service mode only) — `rfq/generator.py` with hard stops.
7. **Persist** — evidence ledger, traces, audit log, episodic memory.

## Determinism

Classifier and extractors are regex/heuristic — no LLM in the hot path — so runs
are reproducible and unit-testable. Providers are injected, so the whole
pipeline runs offline with mock providers.

## Import discipline

Internal modules import submodules directly (e.g. `..agent.budget`) rather than
package `__init__` aggregations, avoiding cycles. Services type-hint the budget
tracker under `TYPE_CHECKING` and duck-type it at runtime.
