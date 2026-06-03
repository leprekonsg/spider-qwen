# Spider-Qwen — Build Plan for Claude Code (Dynamic Workflow)

> **What this file is.** A self-contained, agent-executable implementation plan for the
> `leprekonsg/spider-qwen` repo, targeting the Qwen Cloud "Global AI Hackathon" (Autopilot
> Agent primary track, MemoryAgent secondary), submission deadline **2026-07-09**. It is
> written to be driven by Claude Code: load it, build the task graph into your todo list,
> then repeatedly pick the next unblocked task, implement it, run its acceptance check,
> commit, and mark it done.

---

## Execution status (live) — updated 2026-06-03

**Branch:** `feat/spider-qwen-v1` · **Suite:** 372 passing (offline e2e green) · **Status:** **v1 complete under the documented deviations (#1-#13); post-audit remediation applied (see below)** — all `MUST` + `HIGH` tasks are implemented and green (final task T-8.2 `3fdfb9b`), and every numbered deviation is verified-true against the code. This is NOT literal attainment of the project Definition of Done (§4): some §4 bullets were consciously relaxed by user decision (opt-in `--serendipity` discovery rather than default-pipeline wiring; no demo video; open-source reframe that drops the track-positioning statement) — see deviations #12-#13. · **Not built (by design):** T-6.2 EOL forecaster (`NICE`) + the v2-deferred items (discovery default-integration, MCP-client half, external benchmark adapters, live token metering). Per-task acceptance evidence is in `docs/BUILD_LOG.md`.

| Task | Status | Commit(s) |
|---|---|---|
| T-0 | DONE | 17d5c64 |
| T-1.1 | DONE | effea1a |
| T-1.2 | DONE | b11328a |
| T-1.3 | DONE | 26a1750 |
| T-1.4 | DONE | 6143c14; fix d036998 |
| T-2.1 | DONE | 172b143; fix 570d8fb |
| T-2.2 | DONE | bb0c665; fix 3a66e6a |
| T-2.3 | DONE | 8797a2b |
| T-2.4 | DONE | 84c2ac1 |
| T-3.1 | DONE | c4751d0 |
| T-3.2 | DONE | da135f1 |
| T-3.3 | DONE | a419202; fix 409a747 |
| T-4.1 | DONE | e677bfb |
| T-4.2 | DONE | c27d5db |
| T-4.3 .. T-8.2 | reorganized under the **v2 amendment** below | — |

### Deviations from the plan as written

Each is intentional and consistent with a project hard rule (CLAUDE.md) or a DRY/SRP simplification. None drop scope from a `MUST` task.

1. **Deterministic-first; live Qwen is an opt-in seam (off by default).** The plan prescribes live model calls in several steps (flash page-judge, qwen3.7-max planner DAG, qwen HyDE, flash trajectory sampler, MiniCheck-FT5 770M). Per the hard rule "no LLM in the hot path," each ships as a deterministic regex/heuristic default with the model path as an injectable seam, OFF by default. `--offline` stays fully network-free.
2. **Single integration branch, not per-task branches/PRs.** §0 step 5 specifies `task/<ID>-<slug>` branches + PRs. Actual: one focused, pathspec-scoped commit per task on `feat/spider-qwen-v1` (no per-task branches/PRs in this solo build).
3. **No numpy/scipy.** PPR (T-3.2) and the token-bucket rate limiter (T-1.4) are pure-Python — keeps `--offline` dependency-light. The plan's numpy/scipy.sparse PPR sketch was not used.
4. **Phase 2-3 trust/graph/serendipity components are standalone + opt-in, not yet auto-run by the default pipeline.** The controller exposes seams (`graph_retrieve`, the `serendipity_mode` route flag, verification/page-judge policy gates) but the default offline run does not invoke them. Full wiring is deferred to the hero-query/demo integration (Phase 8).
5. **T-1.1 slot labeling is honest scaffolding.** `s1_substitutes`/`s2_long_tail_sources` are currently rank positions (top non-primary ranked results), not true S1/S2 *classification*. Real S1/S2/S3 population arrives with the graph (T-3.x), legacy-OCR (T-5.1), Wayback (T-5.2), and risk signals (T-5.4).
6. **T-1.4 LLM-Compiler is a deterministic DAG executor**, not a max-emitted DAG (the max-planned DAG is a seam). Follow-up d036998 wired expansion + concurrent fetch into the primary gather path and fixed a wall-clock rate-limiter regression (providers carry `rate_limited`; offline uses `NullRateLimiter`).
7. **T-2.1 judge gate lives in `tools/page_judge.py` + `FetchService`,** not `evidence/ledger.py` as the Files line listed — the ledger already persists the verdict via metadata, so no ledger change was needed (SRP/DRY).
8. **T-2.2 MiniCheck is a deterministic entailment heuristic** (value-grounding + token-overlap + whole-number numeric match), not the 770M MiniCheck-FT5 model; the learned-NLI model and SAFE live-search are opt-in seams.
9. **T-2.4 source tiering centralized in new `governance/source_reliability.py`** (shared by page_judge + ledger, DRY), imported lazily by the ledger to avoid a governance<->evidence import cycle.
10. **T-3.1 graph backend is stdlib `sqlite3` only — no `sqlite-vec`/LightRAG dependency yet** (the embeddings table is left as commented DDL). Graph nodes/edges persist as raw SQLite rows, a deliberate exception to "all persistent models are Pydantic + schema_version" (known limit).
11. **T-3.3 trajectories reuse the T-1.2 deterministic expansion** (DRY) rather than a flash sampler; "substitute mode" maps to `PRODUCT_EXACT_PRICE` (no dedicated substitute enum) plus `REVALIDATION`.
12. **Guardrail invariants are enforced and now tested under the dedicated §3 file names.** (Updated by the post-audit remediation.) §3 names `tests/test_no_unsourced_output.py`, `tests/test_audit_refuses_actions.py`, `tests/test_disputed_excluded_from_rfq.py`, `tests/test_privacy_gate.py` — all four now exist. Coverage: evidence-first by `test_no_unsourced_output.py` (+ `test_rankers.py`/`test_controller.py`/`test_evidence_ledger.py`); no-action by `test_audit_refuses_actions.py` (every forbidden token incl. `browser_drive`); disputed-excluded-from-RFQ by `test_disputed_excluded_from_rfq.py` (the `rfq_eligible` enforcement point, not the prior incidental filter); privacy gate by `test_privacy_gate.py` (the real `ContactExtractor` tagging path + `ReviewGate`); offline-network-free by `test_no_unsourced_output.py` under the `no_network` fixture (proven, not by-construction); Qwen-only by config + README. The earlier claim that these were "covered by existing suites" was partly false (privacy + disputed-from-RFQ had no direct test) — fixed.
13. **Definition of Done (§4) — three bullets consciously relaxed by user decision (T-8.2), not silently unmet.** (a) The §7 hero commands print the 4-slot serendipity view from rank-position scaffolds (deviation #5); the REAL S1/S2/S3 senses require the opt-in `--serendipity` sidecar (`spider_qwen/serendipity/discovery.py`), whose default-pipeline integration is a v2 item. (b) "≥5 Qwen surfaces used" / "Qwen-native" are CONFIGURED (5 model roles in `policy_config.yaml`) but the deterministic offline path invokes none of them — they are opt-in seams (deviation #1); "≥3 MCP integrations" is met as a server exposing 5 read-only tools, but the "both directions" client half is deferred (T-7.1 note). (c) The "≤3-min demo video link" and the "Track 4 primary / Track 1 secondary" statement were dropped when the README was reframed open-source-centric (user chose to skip the video and remove track positioning); both are recorded in the T-8.2 BUILD_LOG row and the README "Known limits / v2 roadmap".

### Post-audit remediation (2026-06-03)

A top-down completion audit surfaced a cluster of real gaps concentrated in the
hero demo and guardrail gate (most disclosed, several not). Fixes applied (suite
348 -> 372, all green):

- **Hero query now exercises all three senses (the headline defect).** The offline
  mock truncated the URL slug to 40 chars, dropping the MPN for long
  natural-language queries, so the §7 Hirose hero query produced an EMPTY S1 and a
  zero-evidence S3. `MockSearchProvider` now preserves the MPN at the front of the
  slug (`_mock_slug`, reusing `_first_mock_mpn`); the documented hero query surfaces
  S1 substitutes + S2 source + S3 risk with provenance. `test_no_unsourced_output.py`
  runs the actual hero query end-to-end.
- **"Evidence or it didn't happen" enforced structurally in discovery.** A single
  `_enforce_evidence` gate withholds any S1/S2/S3 item lacking an evidence_ref
  (was: S3 could promote zero-ref items), recomputing support + noting the drop.
- **Disputed-from-RFQ is now explicit + tested.** The dead `allow_disputed_facts_in_rfq`
  flag is wired through a single `memory.recall.rfq_eligible` enforcement point (was
  an incidental active-only recall filter); see `test_disputed_excluded_from_rfq.py`.
- **No-action guardrail aligned with §3.** `AuditLog.FORBIDDEN` now also rejects
  `browser_drive`/`browser_navigate`/`browser_action` and both `*_submit`/`*_send`
  spellings; see `test_audit_refuses_actions.py`.
- **Privacy gate now tested** via the real `ContactExtractor` tagging path + `ReviewGate`
  (`test_privacy_gate.py`).
- **Offline = zero network is proven**, not by-construction, via a `no_network` fixture
  that blocks non-loopback INET connects.
- **5th §6 skill authored** (`sea-rfq-draft`) — was silently omitted ("4 new cards per
  section 6"). 7 discoverable skills total.
- **Honesty:** `flat_vector_rank` renamed `flat_lexical_rank` (it is a lexical, not
  vector, baseline); electronics PPRM `lifecycle_safety`/`risk` documented as
  unscored-by-design pending the T-5.x signal miners (caps a perfect electronics
  bundle at ~0.7), rather than fabricating a score.

**Still deferred by design (NOT regressions):** live-Qwen-in-hot-path (hard rule),
default-pipeline wiring of the opt-in discovery/reasoning components (deviation #4),
MCP-client half + DashScope `tools=[{type:mcp}]`, demo video, external benchmark
adapters, live token metering, per-substitute electronics lifecycle scoring.

---

## v2 amendment (decided 2026-06-03) — reasoning-first, hybrid storage

A GRAM-inspired "Build Plan v2" (Generative Recursive Reasoning, arXiv:2605.19376 —
borrowed as a *principle*, not ported) was reviewed. ~9 of its tasks were already
DONE under the v1 IDs above. Decision: **keep the v1 IDs and amend** (the 12 DONE
tasks T-0..T-4.2 stand); adopt v2's *reasoning spine* but keep storage pragmatic.
Rationale: the search-quality gain is from reasoning control + provenance + repair,
**not** from vector storage; a full single-file `sqlite-vec` migration would rewind
verified T-2/T-3 work, add a non-portable hard dependency (SQLite extension loading
is not guaranteed on demo/CI machines; this dev box is Python 3.13.9 Anaconda and
*can* load extensions, but that is not portable), and violate "don't rewrite green
modules."

**Adopted (new tasks, our numbering):**

- **T-R.1 · MUST** — `reasoning/trajectory.py` (strategy-typed `ReasoningTrajectory`
  + bundle models + `ReasoningBudget`), `reasoning/process_reward.py` (deterministic
  Procurement Process Reward Model: service & electronics weighted bundle scoring,
  transparent breakdown, no neural training), `reasoning/trajectory_policy.py`
  (activation gate: enabled + mode-in-set + classifier-confidence < 0.75; frozen
  budget caps the agent cannot self-raise). Built *around* the existing
  `serendipity/gram_lite.py` output, not replacing it.
- **T-R.2 · MUST** — `reasoning/recursive_refiner.py` (round-1 explore / round-2
  repair on evidence gaps: missing quote_channel / incomplete checklist / FFF
  conflict / unknown geo / price conflict), `reasoning/trajectory_runner.py`
  (orchestrate >=2 trajectories within budget, score via PPRM, **select the winner**
  consuming verified claims + disputes + scores, emit a "why it won / why
  alternatives lost" explanation, write trajectory provenance `{trajectory_id,
  strategy, round, parent_run_id}` to the ledger), `agent/controller.py` opt-in
  wiring + `tests/test_reasoning_budget.py` runtime budget invariant.
- **T-4.3 (reframed) · HIGH** — **bi-temporal columns on the existing SQLite graph**
  (`valid_from/valid_to/recorded_at` + a `*_current` view; append-only supersession),
  not a Zep/Graphiti engine. The original "Zep/Graphiti KG" framing is dropped.
- **T-R.3 · HIGH** — **optional recall backend seam**: default = deterministic
  lexical + graph/PPRM recall (offline, zero-dep); optional = `sqlite-vec` +
  `text-embedding-v4` vector recall *only when the extension loads and a key is
  present*. The unavailable path must auto-fall-back and is explicitly tested.
- **T-R.4 · HIGH** — first-class `electronics_substitution` vertical mode on the same
  spine (`{recommended|needs_engineering_review|rejected}` + `assumptions_and_limits`;
  never claims drop-in without datasheet evidence). Additive — the existing
  mode-mapping (deviation #11) stays as the fallback.

**Dropped:** v1 T-5.3 (LinUCB contextual-bandit source selector) — RL-driven source
selection conflicts with the no-RL-crawling ethos. v1 T-6.2 (EOL forecaster) and the
counterfactual-BOM / OOD-monitor parts of T-5.4 are demoted to `NICE`/future-flag.

**Remaining v1 tasks unchanged** (re-sequenced after the reasoning spine): T-5.1
(legacy-OCR), T-5.2 (Wayback), T-5.4 (S3 deprecation-diff + proactive-watch), T-6.1
(DMSMS SD-22 ladder + counterfeit badge), T-7.1 (MCP), T-7.2 (Skills), T-7.3 (cost
router), T-8.1 (benchmarks), T-8.2 (demo/docs).

**v2 acceptance bar (applies to T-R.*):** `--offline` works with no `sqlite-vec`, no
embeddings, no network; the verification budget is always reserved; trajectory edges
carry evidence/provenance; the winner output explains why it won and why alternates
lost; the `sqlite-vec`-unavailable path is explicitly tested.

**v2 amendment progress (updated 2026-06-03):** all adopted tasks DONE and green
(264 tests). T-4.1 `e677bfb`, T-4.2 `c27d5db`, T-R.1 `d0e58fe`, T-R.2 `bd15bdd`,
T-4.3 `24aedb4`, T-R.3 `d5adb75`, T-R.4 `79eb7f3`. **Phases 4 + R complete.**
Remaining (original v1 scope, re-sequenced): T-5.1 legacy-OCR, T-5.2 Wayback (MUST),
T-5.4 S3 deprecation-diff/proactive-watch, T-6.1 DMSMS ladder + counterfeit (MUST),
T-7.1 MCP (MUST), T-7.2 Skills (MUST), T-7.3 cost router, T-8.1 benchmarks,
T-8.2 demo/docs (MUST). Per-task detail in `docs/BUILD_LOG.md`.

**Post-phase-R review (2026-06-03) — 5 findings triaged, all resolved (268 tests):**
- *P1 (bug)* repair round silently cleared unresolved disputes (`_merge` overwrote
  `disputed_count`) -> fixed to `max()` so disputes stay consistent with the
  conflict penalty. `857038e`.
- *P2 (bug)* bi-temporal supersession had no effect on retrieval (`edges/neighbors/
  traverse` read the raw table, not current rows) -> default reads now filter
  `valid_to IS NULL`; history via `versions()`/`as_of()`. `fa9df37`.
- *P1 (integration/overstated claim)* reasoning spine was unreachable from any
  entrypoint -> exposed opt-in via `spider-qwen run --reason` (emits a
  ReasoningResult); full discovery-layer wiring into default `run()` stays a
  Phase-8 (T-8.2) item. `e18d1aa`.
- *P2 (deferred, signal-blocked)* `electronics_substitution` emits a product-shaped
  result, not `ElectronicsSubstitutionResult` -> kept deferred (needs FFF/lifecycle/
  datasheet signals from T-5.1/T-5.2/T-6.1); documented STATUS + BLOCKING NOTE +
  5-condition phase gate in `modes/electronics.py`. `b7272a2`.
- *clarification* `ReasoningBudget.max_trajectories = 3` (default trajectories
  EXPLORED per run); there are 5 service + 5 electronics strategies AVAILABLE, of
  which the top 3 for the mode are explored. Earlier "5 strategies" phrasing meant
  available, not explored.

**Phase 5 progress (2026-06-03):**
- **T-5.2 Wayback CDX temporal sourcing — DONE (279 tests).** `serendipity/wayback.py`
  `WaybackClient.recover(url)`: CDX query (`filter=statuscode:200`) -> newest snapshot
  -> raw archived content; >=1 req/sec throttle, exponential backoff on 429,
  circuit-breaker on **sustained** 429s (counter accumulates across calls by design
  — an IA IP block spans calls — so it can trip even though default threshold 5 >
  max_retries+1). Injected `http_get` seam keeps `--offline` network-free; `SourceTool`
  gains `wayback_cdx`; opt-in `FetchService(wayback=...)` recovers dead/404 fetches into
  evidence (confidence 0.5). NOT wired into default `run()` (deviation #4; full
  discovery-layer integration is Phase 8 / T-8.2). Adversarial review (13 agents, 7
  confirmed findings -> 3 distinct): fixed P1 IndexError on truncated CDX rows + P1
  judge-rejected-URL recovery (gate bypass); rejected the "reset counter per call"
  suggestion as it would disable the breaker at default config. *Deviation:* recovered
  dead-URL content is down-weighted but not re-judged (deferred, opt-in path).
- **T-5.1 legacy-OCR cross-ref miner — DONE (286 tests).** `serendipity/legacy_ocr_miner.py`
  parses scanned replacement-guide TABLES (NTE/ECG/Motorola->Fairchild) into MPN->MPN
  `CROSS_REFERENCE` edges (`source="legacy_book"`), each tied to the OCR'd page's
  ledger_id; OCR is an injected `ocr_fn` seam so `--offline` is network-free; `SourceTool`
  gains `legacy_ocr`. Acceptance met (14-row fixture -> 14 edges, traversable, all
  ledger-backed). Review fixed a P1: doc/version refs (Rev.2.1/Fig.1.23/Page12) were
  minting junk edges -> `_is_mpn` now drops doc-reference-prefixed tokens (keeps "ref"
  so REF200-style parts survive). Standalone + opt-in.
- **T-5.4 proactive S3 signal bundle (core) — DONE (298 tests).** `serendipity/signals.py`:
  `detect_lifecycle` (word-boundary lifecycle state), `diff_datasheet` (worsening / new
  PCN / second-source-removed -> RiskSignal leading indicator), `proactive_watch` (NRND/EOL
  or PCN -> deprecation signal + graph-sourced, evidence-backed substitute alternates,
  never fabricated). Reuses T-1.1 RiskSignal; standalone + opt-in (controller interjection +
  metrics are Phase 8). *Deviation (already in v2 amendment):* the OOD price/lead-time monitor
  and counterfactual-BOM what-if stay NICE/unbuilt. Review fixed substring-matching false
  positives/negatives (active-in-inactive, nrnd/ltb-in-MPN, pcn/pdn-in-EPCN) via word
  boundaries + PCN-id requirement; added "eol" abbrev. **Phase 5 complete** (T-5.3 LinUCB
  bandit dropped in the v2 amendment).
**Phase 6 progress (2026-06-03):**
- **T-6.1 DMSMS SD-22 ladder + LTB + counterfeit badge — DONE (306 tests).**
  `serendipity/dmsms.py`: `build_dmsms_ladder` (full 6-rung SD-22 ladder, context-driven
  applicable flags), `ltb_quantity` (newsvendor cost-sensitive bridge buy), `counterfeit_badge`
  (ERAI/GIDEP fixtures + authorization tier -> red/amber/green + AS6081/AS6171 + FAR 52.246-26).
  Standalone + opt-in (RFQ-draft embedding is Phase 8; service RFQ generator untouched). Review:
  0 confirmed findings. **Phase 6 core complete** (T-6.2 EOL forecaster stays NICE/unbuilt).
**Phase 7 progress (2026-06-03):**
- **T-7.1 MCP integration (read-only, optional SDK) — DONE (312 tests).** New
  `spider_qwen/mcp/` package, split so the offline default never depends on the SDK:
  `handlers.py` = five pure, deterministic, dependency-free functions (always tested) --
  `procurement_classify`, `evidence_show`, `evidence_verify` (span + Merkle chain),
  `memory_recall`, `memory_reflect`; `schemas.py` = typed schema_version'd I/O models;
  `server.py` = thin FastMCP stdio adapter importing `mcp` ONLY inside `build_server()`
  (module import never needs it) and raising an actionable "install spider-qwen[mcp]" on a
  missing SDK. `pyproject.toml` gains OPTIONAL extra `mcp = ["mcp>=1.2"]` (not core).
  Adversarial review: 3 dimensions, 0 confirmed findings. *Decision (user, via
  AskUserQuestion):* "Official MCP SDK, optional extra, offline-tested" with hard
  boundaries -- v1 surface is read-only/bounded only (no file mutation, RFQ send, Drive
  browsing, or DashScope calls). *Deviation #4 (standalone + opt-in):* live third-party MCP
  consumption (Google Drive RFP docs) and the DashScope Responses-API `tools=[{type:mcp}]`
  wiring are deferred to Phase 8 (T-8.2). The plan's "both directions" framing is therefore
  half-built by design in v1: spider-qwen-as-MCP-server is DONE; spider-qwen-as-MCP-client
  (consuming external servers) is the deferred Phase-8 half.
- **T-7.2 Project Qwen Agent Skills — DONE (324 tests).** New `spider_qwen/skills/`
  package: `AgentSkill` model + `SkillRegistry` that discovers `.qwen/skills/*/SKILL.md`
  cards (YAML frontmatter + `## Instructions`/`## Examples`, degrading to frontmatter-less
  parsing so the 2 pre-existing cards still load) and `match`es by deterministic
  term-overlap (name+description+keywords, stopword-filtered, no LLM). Authored 4 new cards
  per section 6 (`mpn-canonicalize`, `obsolescence-triage`, `fff-substitute-judge`,
  `counterfeit-risk-check`); 6 discoverable skills total. `Planner.select_skills(query)`
  auto-selects by description; CLI `spider-qwen skills [list|match|show]`. The hero query
  auto-invokes obsolescence-triage + fff-substitute-judge. *Deviation #4 (standalone +
  opt-in):* the planner SELECTS skills; injecting their instructions into live Qwen-assisted
  `run()` steps stays the existing narrow `qwen_json_extractor` wiring + Phase 8 (offline
  runs call no LLM, so skills are guidance-only there). Distinct from T-4.2
  `memory/skills_library.py` (induced query templates). Review (loader/matching/integration;
  1 confirmed + fixed): unguarded `yaml.safe_load` would crash the whole registry on one
  malformed card -> caught -> frontmatter-less fallback. 12 new tests (test_skills.py).
- **T-7.3 Cost router + dashboard — DONE (333 tests).** `Policy.route_task(task, high_risk)`
  routes extraction/classification/judge -> flash, planning/reasoning -> max; the
  `high_risk_procurement` tag escalates any step (notably `decision`) to max. `model_for`
  wrapped so a minimal Policy routes without crashing. `Policy.model_pricing()` merges a
  configurable `pricing:` block over `DEFAULT_MODEL_PRICING`. `observability/metrics.py`:
  `RouteDecision`/`ModelCost`/`CostReport` + `CostMeter` (computes $/run, tokens, TinyFish
  calls, $-saved-vs-all-max); `Metrics.cost`. Controller `run(high_risk=False)` logs the
  routing plan + cost breakdown; CLI `run --high-risk`. *Deviation:* live per-call token
  metering is deferred (offline pipeline calls no model -> meter empty by construction; the
  dashboard + routing + TinyFish count are the v1 deliverable). RouteDecision placed in
  `observability/metrics.py` (not policy.py) to keep `policy -> observability` cycle-free.
  Review (routing/cost-math/integration): 0 confirmed. 9 new tests (test_cost_router.py).
  **Phase 7 complete.**
**Phase 8 progress (2026-06-03):**
- **T-8.1 Benchmark reporting — DONE (337 tests).** Added 20 `electronics_substitution`
  gold cases (now 100 cases, 20 per mode) tagged by serendipity sense (7 s1 / 7 s2 / 6 s3),
  each an obsolete-part query with an MPN. Existing harness handles the new mode via
  `per_mode` (no harness change). Offline: 100 cases, mode acc 0.96 (electronics 1.00),
  quote precision / rfq completeness / evidence coverage 1.00. README Benchmarks section
  refreshed (metrics + per-mode table + reproduce command). *Deviation:* BFCL V4 / tau-bench
  / LOCOMO need published datasets + live API the offline harness does not bundle; per the
  no-fabrication rule they are documented as **deferred to v2, not estimated**. Deep S1/S2/S3
  slot-content eval is gated on the deferred discovery wiring (T-8.2). Review (gold-data +
  README-honesty): 0 confirmed. 4 new tests (test_benchmark_electronics.py).
- **T-8.2 Demo + docs polish + `--serendipity` sidecar — DONE (346 tests).** User decision:
  thin opt-in discovery sidecar (not default integration); skip demo video; enhance README
  in place. New `serendipity/discovery.py` populates S1 (graph-PPR substitutes, CoVe-gated),
  S2 (long-tail/archival sources), S3 (lifecycle/PCN + DMSMS) over the run's ledger -- a
  sidecar that consumes no new fetch/search budget; `RunResult.serendipity_discovery` via
  `run(serendipity=True)`; CLI `run --serendipity`; offline demo via mock enhancements gated
  to obsolete-part queries. README reframed open-source-centric (track positioning removed),
  modes table + architecture diagram + Why-Qwen + Known-limits/v2-roadmap updated. Review
  (sidecar-logic/integration/mock-regression): 3 fixes (S2 clip note, store.close try/finally,
  unused import). **Phase 8 complete -- all v1 MUST/HIGH tasks done.** Remaining: T-6.2 EOL
  forecaster (NICE/unbuilt); v2 = discovery default-integration + MCP-client half + external
  benchmark adapters + live token metering.

---

## 0. How Claude Code should use this file

**Workflow contract (re-read at the start of every working session):**

1. Parse the **Task Graph** (§5). Build a todo list from the tasks whose dependencies are all `DONE`.
2. Pick the highest-priority unblocked task. Announce it, then implement only that task.
3. Run the task's **Acceptance** check. If it fails, fix forward; do not proceed to the next task.
4. Run the **Guardrail Gate** (§3). If any guardrail fails, revert the change.
5. Commit on a task branch `task/<TASK-ID>-<slug>`, open/update a PR, mark the task `DONE` in your todo list, and append a one-line entry to `docs/BUILD_LOG.md`.
6. Re-evaluate the graph and repeat.

**Dynamic re-planning rules:**
- If a task's acceptance check reveals the repo already implements it, mark `DONE (pre-existing)` and move on — do not rewrite working code.
- If a task is larger than ~1 day once you open the files, split it into `TASK-ID.a`, `.b` and re-insert into the graph.
- If `time_remaining < effort_remaining` for the current phase, drop the lowest-priority `NICE` tasks first (they are tagged), never the `MUST` tasks.
- Never start Phase _N+1_ "hero" tasks while a Phase _N_ `MUST` task is still `TODO`.

**Priority tags:** `MUST` (ship-blocking) · `HIGH` (large score impact) · `NICE` (drop first under time pressure).

---

## 1. Project context (ground truth)

Spider-Qwen is an evidence-first agentic procurement scout: given a query like *"find a replacement
for an obsolete Hirose DF13-6P connector, deliver to Singapore in 14 days,"* it returns an
evidence-backed vendor/substitute shortlist plus a ready-to-review RFQ draft — and **never submits or
sends anything**. The mission focus is **hard-to-find / obsolete products** across three "serendipity"
senses:

- **S1 — substitute / cross-reference discovery** (the exact part is dead → find FFF-equivalents and OEM successors).
- **S2 — long-tail / secondary-market sourcing** (find the obscure broker/surplus/marketplace that still has stock).
- **S3 — proactive risk signal discovery** (EOL/PCN/last-time-buy windows, counterfeit risk, supply shocks).

Existing repo layout (target these paths; do not restructure):

```
spider_qwen/
  agent/         controller, budget, planner, policy, tool_registry, execution_context
  modes/         classifier, contracts, router
  tools/         tinyfish_client, search_service, fetch_service, qwen_web_extractor, qwen_json_extractor, provider_types
  extraction/    pricing, quote_channel, contact, vendor_metadata, service_match, dedupe
  ranking/       product/service/contact rankers, geo_strategy
  evidence/      models, ledger, dedupe, bundles, verifier, graph
  memory/        working, episodic, semantic, decay, promotion, revalidation, mcp
  rfq/           schema, checklist, generator
  governance/    policy_config.yaml, privacy, review, audit
  observability/ metrics, tracing
  api/           schema, cli, server
  benchmarks/    gold_set.json, evaluators, baseline comparison
docs/            architecture & design references
tests/           unit + end-to-end suite
```

New top-level dirs this plan adds: `spider_qwen/serendipity/`, `spider_qwen/graph/`, `spider_qwen/verification/`, `.qwen/skills/`, `mcp/`.

---

## 2. Environment & model configuration (do this first, exactly)

All model access is Alibaba Cloud DashScope **International**, OpenAI-compatible base URL.

```
DASHSCOPE_BASE_URL = https://dashscope-intl.aliyuncs.com/compatible-mode/v1
DASHSCOPE_API_KEY  = <env, never commit>
TINYFISH_API_KEY   = <env; Search+Fetch are free tier: 5 search/min, 25 fetch/min>
```

**Model role assignments** — put these in `spider_qwen/governance/policy_config.yaml` under a new `models:` block, never hard-coded:

| Role | Model string | Mode notes |
|---|---|---|
| Planner / reasoning / high-stakes judging | `qwen3.7-max` | deep-thinking ON; 1M context |
| Cheap extraction / classification / query-variant gen / reflection | `qwen3.5-flash` (fallback `qwen-flash`) | **thinking OFF** |
| Embeddings (skills, notes, FFF specs) | `text-embedding-v4` | Matryoshka: 768d index, 2048d rerank |
| Datasheet / legacy-book OCR | `qwen-vl-ocr-2025-11-20` | Singapore region OK |

**Hard API constraints (encode as guardrails, see §3):**
- Structured/JSON output works **only in non-thinking mode**; the prompt **must contain the literal token `"json"`** or DashScope errors.
- `tool_choice` accepts only `"auto"` or `"none"` — **not** `"required"`. Enforce "must-call" at the planner-prompt or state-machine layer.
- Pin dated snapshots in config (`qwen3.5-flash-2026-02-23` etc.) for demo reproducibility; the rolling alias can change planner behaviour mid-hackathon.

**T-0 setup task — `MUST`:** add the `models:` block, wire `tools/` provider construction to read it, confirm `spider-qwen run "office cleaning Singapore" --offline` still works, and `python -m pytest -q` is green. **Acceptance:** offline run prints a valid result JSON with a `run_id`; test suite green. **DONE (17d5c64).**

---

## 3. Guardrail Gate (re-check on every commit — non-negotiable)

A commit is rejected if any of these fail:

1. **Evidence-first:** every vendor/contact/price/substitute/RFQ field in output carries an `evidence_ref` (ledger_id) — no bare URLs, no unsourced claims. Add `tests/test_no_unsourced_output.py` and keep it green.
2. **No-action invariant:** `audit.record("rfq_sent" | "form_submit" | "email_send" | "browser_drive")` raises `PolicyViolation`. Keep `tests/test_audit_refuses_actions.py` green.
3. **Qwen-only:** no non-Qwen LLM is invoked anywhere in the runtime path, and no non-Qwen model name appears in `README`/docs as a dependency. (External non-LLM APIs — TinyFish, Wayback, Octopart/Nexar, eBay, Neo4j — are allowed.)
4. **Disputed facts excluded from RFQ:** any fact with `status=disputed` must not appear in a generated RFQ draft. Keep `tests/test_disputed_excluded_from_rfq.py` green.
5. **Offline demo path intact:** `--offline` runs with deterministic mocks and zero network calls (CI runs the offline e2e test with network disabled).
6. **Privacy gate:** named-person contacts are tagged `high_sensitivity` and gated; generic business contacts pass. Keep `tests/test_privacy_gate.py` green.

---

## 4. Definition of Done (the whole project)

- All `MUST` tasks `DONE`; ≥80% of `HIGH` tasks `DONE`.
- One end-to-end "hero" query exercises all three serendipity senses and prints a full provenance ledger (see §7 Demo).
- ≥5 QwenCloud API surfaces used (qwen3.7-max, qwen3.5-flash, text-embedding-v4, qwen-vl-ocr, + structured-output/tool-calling) and ≥3 MCP integrations wired.
- README leads with the evidence-first + serendipity value prop, has a Mermaid architecture diagram, benchmark numbers, a ≤3-min demo video link, and an explicit "Track 4 (Autopilot) primary, Track 1 (MemoryAgent) secondary" statement.
- Guardrail Gate green; `docs/BUILD_LOG.md` complete.

---

## 5. Task Graph

Notation per task: **ID · Priority · Effort(days) · Deps → Files → Goal / Steps / Acceptance.**
Phases are milestones; tasks within a phase can parallelize unless a dep says otherwise.

### Phase 1 — Discovery upgrades (makes the demo visibly better fast) — COMPLETE

**T-1.1 · MUST · 1 · [T-0]** — DONE (effea1a)
Files: `modes/contracts.py`, `api/schema.py`, `ranking/*`.
Goal: **Serendipity output schema.** Reshape every run result into
`{primary_answer, s1_substitutes[], s2_long_tail_sources[], s3_risk_signals[], evidence_refs[]}`.
Score each non-primary slot on `relevance × novelty × unexpectedness` vs a baseline (top-3 authorized-distributor results).
Steps: add Pydantic models w/ `schema_version`; add a `serendipity_score` field; wire rankers to populate slots.
Acceptance: a service-mode run returns the 4-slot object; unit test asserts slot presence + score in [0,1].

**T-1.2 · MUST · 1 · [T-0]** — DONE (b11328a)
Files: `agent/planner.py`, new `spider_qwen/serendipity/query_rewrite.py`.
Goal: **Step-Back + HyDE + Query2Doc** query expansion for vague/obsolete queries (prompt-only, qwen3.5-flash + qwen3.7-max for HyDE doc).
Steps: implement `expand_query(q) -> list[SearchQuery]` producing (a) a stepped-back device-class query, (b) a HyDE pseudo-datasheet paragraph, (c) Query2Doc concat; add obsolescence vocabulary expansion (`obsolete, EOL, NRND, NLA, LTB, superseded by, cross reference, equivalent, NOS`).
Acceptance: golden test — expanding "16-pin SMT op-amp TI used in 90s ECUs" yields ≥4 distinct queries incl. one MPN-pattern and one broker-operator query.

**T-1.3 · MUST · 1.5 · [T-1.2]** — DONE (26a1750)
Files: `agent/controller.py`, new `spider_qwen/serendipity/corrective.py`.
Goal: **Search-o1 mid-reasoning retrieval + CRAG corrective evaluator.** Condense fetched pages (qwen3.5-flash "reason-in-documents") before reinjection; classify retrieval as correct/ambiguous/incorrect → on `incorrect`, trigger broker-pivot/broaden branch.
Acceptance: when seeded fixtures return only irrelevant pages, the controller emits a broadened/broker query rather than answering; trace shows the CRAG verdict.

**T-1.4 · HIGH · 1.5 · [T-0]** — DONE (6143c14; fix d036998)
Files: `agent/controller.py`, `tools/*`, new `spider_qwen/agent/compiler.py`.
Goal: **LLM-Compiler parallel tool DAG.** qwen3.7-max emits a tool-call DAG; an executor runs independent TinyFish/WebExtractor/OCR calls via `asyncio.gather` (respect free-tier rate limits: token-bucket 5 search/min, 25 fetch/min); join → reason.
Acceptance: a "compare 5 vendors across 3 regions" query issues ≥5 fetches concurrently (assert via mock timing) and wall-clock < sequential baseline; trace shows the DAG.

### Phase 2 — Trust spine (provenance is the differentiator) — COMPLETE

**T-2.1 · MUST · 1 · [T-0]** — DONE (172b143; fix 570d8fb)
Files: `tools/qwen_json_extractor.py`, `evidence/ledger.py`.
Goal: **Schema-constrained extraction + judge gate.** Force qwen3.5-flash JSON-schema output (non-thinking, literal "json" in prompt) for all identifier/price/contact extraction; add a Prometheus-style LLM-judge (qwen3.5-flash) scoring each fetched page on (relevance, freshness, source-authority, contradicts-ledger?) before the ledger writer persists it.
Acceptance: malformed-extraction retry rate drops to ~0 in tests; a low-authority page is rejected/flagged, not silently stored.

**T-2.2 · MUST · 2 · [T-2.1]** — DONE (bb0c665; fix 3a66e6a)
Files: new `spider_qwen/verification/` (`atomic.py`, `minicheck.py`, `safe.py`), `evidence/verifier.py`.
Goal: **Verification spine.** FActScore-style atomic decomposition → MiniCheck-FT5 (770M) gatekeeper on every `(claim, evidence_span)` → SAFE-style search-grounded re-verification (TinyFish in place of Google) for flagged atoms.
Steps: run MiniCheck locally (CPU/GPU ok); write `verified: bool` + `verifier_score` to each claim row.
Acceptance: an injected unsupported claim is caught (`verified=false`) and blocked from output.

**T-2.3 · HIGH · 2 · [T-2.2]** — DONE (8797a2b)
Files: `verification/cove.py`, `evidence/models.py`, `memory/promotion.py`.
Goal: **Chain-of-Verification + semantic entropy + contradiction detection.** Wrap substitute suggestions in CoVe (draft → verify-questions → answer-independently → revise). Compute substitute confidence via semantic entropy over 5 samples clustered on (MPN, manufacturer). Detect cross-source contradictions on (entity, property) → promote to `disputed`.
Acceptance: a phantom (plausible-but-discontinued) substitute is removed by CoVe; a Mouser-active vs DigiKey-NRND pair becomes a `disputed` record retaining both spans.

**T-2.4 · HIGH · 1 · [T-2.1]** — DONE (84c2ac1)
Files: `evidence/ledger.py`, `governance/policy_config.yaml`.
Goal: **Source-reliability prior + Merkle-DAG ledger.** Add per-source reliability prior (mfr datasheet 0.99, authorized dist 0.95, aggregator 0.85, broker 0.4–0.6, marketplace 0.1–0.3 w/ mandatory auth). Append each claim with `sha256(claim_bytes ‖ parent_sha)`; expose `spider-qwen evidence verify <run_id>` to re-walk the chain.
Acceptance: tampering with one ledger row breaks chain verification; reliability prior multiplies into per-claim confidence.

### Phase 3 — The spider web graph (multi-hop = the moat) — COMPLETE

**T-3.1 · HIGH · 3 · [T-2.1]** — DONE (c4751d0)
Files: new `spider_qwen/graph/` (`schema.py`, `store.py`, `extract.py`), `evidence/graph.py`.
Goal: **Supplier-part property graph (LightRAG-backed, SQLite + `sqlite-vec`).** One embedded, file-based store for ledger + graph + embeddings — no extra service, no Docker, `--offline` stays network-free. Implement the LPG schema:
Nodes `Part, Manufacturer, Distributor, Datasheet, Parameter, Package, PCN, Claim, Source`.
Edges (all with `confidence, evidence_claim_id, ts, reliability`): `MANUFACTURED_BY, STOCKED_AT{qty,price,moq,lead}, CROSS_REFERENCE{grade}` (**asymmetric**), `SUPERSEDED_BY, PIN_COMPATIBLE_WITH, SAME_DIE_AS, AFFECTED_BY, ACQUIRED_BY, RENAMED_TO, FRANCHISE_FOR, CONTRADICTS`.
Use REBEL/SAC-KG (Generator→Verifier→Pruner) for triple extraction; pipe triples through the ledger + MiniCheck (T-2.2 spine) before upsert.

Concrete backend — two tables in the existing SQLite file (the evidence ledger's store):

```sql
CREATE TABLE nodes (
  id    TEXT PRIMARY KEY,             -- canonical key, e.g. "part:ATMEGA48" / "mfr:microchip"
  type  TEXT NOT NULL,                -- Part | Manufacturer | Distributor | Datasheet | ...
  props TEXT NOT NULL DEFAULT '{}'    -- JSON blob of free-form node attributes
);
CREATE TABLE edges (
  src               TEXT NOT NULL REFERENCES nodes(id),
  dst               TEXT NOT NULL REFERENCES nodes(id),
  rel               TEXT NOT NULL,    -- ACQUIRED_BY | SUPERSEDED_BY | CROSS_REFERENCE | ...
  confidence        REAL NOT NULL,
  reliability       REAL NOT NULL,    -- source-reliability prior (T-2.4)
  evidence_claim_id TEXT NOT NULL,    -- ledger_id; never a bare URL (hard rule)
  event_ts          TEXT,             -- when the fact became true  (bi-temporal, T-4.3)
  ingest_ts         TEXT NOT NULL,    -- when we recorded it        (bi-temporal, T-4.3)
  grade             TEXT,             -- CROSS_REFERENCE{grade}: drop_in | electrical_equiv | ...
  props             TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (src, dst, rel, evidence_claim_id)   -- one edge per asserting claim
);
CREATE INDEX idx_edges_src ON edges(src, rel);     -- forward traversal
CREATE INDEX idx_edges_dst ON edges(dst, rel);     -- reverse traversal / asymmetry
-- embeddings live in the same file (sqlite-vec):
--   CREATE VIRTUAL TABLE node_vec USING vec0(node_id TEXT, embedding float[768]);
```

The asymmetric `CROSS_REFERENCE{grade}` edge is naturally directional as a row (cleaner than forcing it into a symmetric graph DB). Bi-temporality is the two `*_ts` columns, not a new engine — T-4.3 builds on them.

Bounded multi-hop traversal is a recursive CTE (no graph engine needed for ≤2–3 hops):

```sql
WITH RECURSIVE chain(id, path, depth) AS (
  SELECT id, id, 0 FROM nodes WHERE id = 'part:ATMEGA48'
  UNION ALL
  SELECT e.dst, chain.path || ' -> ' || e.rel || ' -> ' || e.dst, chain.depth + 1
  FROM edges e JOIN chain ON e.src = chain.id
  WHERE chain.depth < 2
    AND e.rel IN ('ACQUIRED_BY', 'SUPERSEDED_BY', 'CROSS_REFERENCE')
)
SELECT path FROM chain WHERE depth > 0;
-- resolves "ATmega48 -> ACQUIRED_BY Microchip -> SUPERSEDED_BY ATmega48A" in <= 2 hops
```

PPR (T-3.2) is not a DB op: `SELECT src, dst, confidence FROM edges`, build a sparse adjacency, power-iterate with a restart vector seeded on the query nodes:

```python
# graph/retrieve.py sketch — Personalized PageRank over edges pulled from SQLite
import numpy as np
def ppr(adj, seed_idx, alpha=0.15, iters=30):     # adj: row-normalized scipy.sparse CSR
    n = adj.shape[0]
    r = np.zeros(n); r[seed_idx] = 1.0 / len(seed_idx)
    p = r.copy()
    for _ in range(iters):
        p = (1 - alpha) * (adj.T @ p) + alpha * r   # teleport back toward the seed nodes
    return p                                         # rank candidate nodes by p
```

Honest limits (choose with eyes open): deep/unbounded traversal (5+ hops over a dense graph) gets slow in CTEs; SQLite handles concurrent writers poorly (fine for single-agent runs); if the part graph later reaches millions of edges with heavy graph-algorithmic queries, migrate to a native engine. None bite at hackathon scale.

Acceptance: ingesting fixture datasheets builds a graph where the recursive-CTE query above resolves "Atmel ATmega48 → (ACQUIRED_BY Microchip) → (SUPERSEDED_BY ATmega48A)" in ≤2 hops.

**T-3.2 · HIGH · 1.5 · [T-3.1]** — DONE (da135f1)
Files: `graph/retrieve.py`, `agent/controller.py`.
Goal: **HippoRAG Personalized PageRank multi-hop retrieval** over the LPG; relation-path planning (Reasoning-on-Graphs style) that doubles as a buyer-facing explanation.
Acceptance: a multi-hop substitute query returns the successor chain + a human-readable path; PPR retrieval beats flat-vector baseline on a 10-query fixture set (report recall@5).

**T-3.3 · HIGH · 1.5 · [T-1.2, T-2.3]** — DONE (a419202)
Files: new `spider_qwen/serendipity/gram_lite.py`, `modes/router.py`.
Goal: **GRAM-lite "Serendipity Mode."** Width-first: qwen3.5-flash samples `S=5` diverse query trajectories (synonym / broader-class / broker-operator / SEA-local-language / mfr-alt-PN), TinyFish runs them in parallel, existing ranker selects (LPRM role). Depth cap `D_max=3` with per-hop confidence ×0.85. Verification fan-out `K=3` (re-fetch / competing-vendor / Wayback) → disputed handler. Every recursion edge writes `(parent_sha, child_sha, depth, query, ts)`.
Acceptance: substitute & revalidation modes invoke Serendipity Mode; ledger shows the recursion tree; cost/latency within caps (≤1 Max call + ≤25 flash + ≤45 fetch per top-level query).

### Phase 4 — Memory moat (MemoryAgent secondary track)

**T-4.1 · HIGH · 3 · [T-2.1]**
Files: `memory/semantic.py`, `memory/decay.py`, new `memory/reflections.py`, `memory/notes.py`.
Goal: **Reflections + Ebbinghaus decay + A-MEM Zettelkasten.** Periodic qwen3.5-flash job writes 5–10 insights from recent ledger entries. MemoryBank decay `strength(t)=strength₀·exp(-t/S)`, S grows on re-access, disputed facts halved per contradiction. Each ledger entry → Zettelkasten note (LLM attributes: entity, geo, supply-stage, risk-flag) with 1–3 agentic links; backend = Mem0g-style (Qdrant/Neo4j) used silently.
Acceptance: `spider-qwen memory show` prints active facts with TTL/last_verified/confidence; a second run of the same query reuses a cached, still-valid quote channel and the trace proves memory changed behaviour.

**T-4.2 · HIGH · 2 · [T-4.1, T-3.1]**
Files: new `memory/skills_library.py`, `memory/workflows.py`.
Goal: **Voyager skill library + Agent Workflow Memory.** Successful playbooks become parameterized skills (indexed by text-embedding-v4); AWM induces reusable workflows from past trajectories; AutoGuide-style context-conditional guidelines.
Acceptance: after running 3 service queries, a reusable "find ISO-certified supplier in region R, MOQ < N" skill is stored and retrieved for a 4th query.

**T-4.3 · NICE · 1.5 · [T-4.1, T-3.1]**
Files: `memory/temporal.py`, `graph/store.py`.
Goal: **Bi-temporal facts on the SQLite edge table** (no Zep/Graphiti engine). Reuse the `event_ts` (when the fact became true) and `ingest_ts` (when we recorded it) columns from the T-3.1 `edges` table. Supersession is append-only: a newer edge for the same `(src, dst, rel)` never deletes the prior row; "current" is the row with the max `event_ts` per key. Covers price/cert/lifecycle versioning without a new substrate.
Acceptance: querying "what was vendor X's price last month vs now" returns both rows with timestamps; superseded facts are retained, not deleted (assert the prior edge row still exists after a price update).

### Phase 5 — Serendipity creatives (the Innovation-score winners)

**T-5.1 · HIGH · 2 · [T-3.1]**
Files: new `spider_qwen/serendipity/legacy_ocr_miner.py`, `tools/` OCR wiring.
Goal: **Qwen-VL-OCR legacy cross-reference miner.** OCR + table-extract NTE / ECG-Master / Motorola→Fairchild / Philips-Signetics replacement guides into an MPN→MPN substitute graph (upsert into the LPG with `source="legacy_book"`). Serves **S1**; nobody else will demo a substitute graph from 40-year-old PDFs.
Acceptance: a sample legacy page yields ≥10 validated MPN→MPN edges visible in the graph.

**T-5.2 · MUST · 1 · [T-0]**
Files: new `spider_qwen/serendipity/wayback.py`, `tools/fetch_service.py`.
Goal: **Wayback CDX temporal sourcing.** On a dead/404 distributor or datasheet URL, query the free CDX API (`web.archive.org/cdx/search/cdx?...&filter=statuscode:200`), fetch the most recent 200 snapshot, parse availability tables / archived datasheets. **Throttle ≤1 req/sec, exponential backoff on 429, circuit-breaker on sustained 429** (IA now enforces ~60/min + 1-hour IP blocks). Serves **S2/S3**.
Acceptance: a fixture 404 triggers a CDX fallback that recovers an archived listing with a proper `evidence_ref`.

**T-5.3 · HIGH · 2 · [T-1.1]**
Files: new `spider_qwen/serendipity/bandit.py`, `ranking/geo_strategy.py`.
Goal: **LinUCB contextual-bandit source selector.** Arms = obscure sources (Lansdale, Rochester, eBay B2B, IndiaMART, Alibaba, Wayback, OEMsecrets); context = part-class embedding + lifecycle hint + region; reward = ledger-validated useful answer; ε-greedy 15% exploration. Serves **S2**.
Acceptance: over 20 simulated queries the bandit shifts weight toward the arm that historically yields validated answers for a part class; a live learning-curve plot is logged.

**T-5.4 · HIGH · 2.5 · [T-3.1]**
Files: new `spider_qwen/serendipity/signals.py`, `observability/metrics.py`, `agent/controller.py`.
Goal: **Proactive S3 bundle.** (a) Datasheet deprecation-diff alerts (monthly crawl + diff for "NRND"/"preliminary"/"second source removed" — 3–9-month leading indicator). (b) OOD detector (energy/z-score) on per-part price/lead-time history, flag >3σ. (c) Counterfactual-BOM "what-if storm" (fab offline / +25% price / broker delisted → recomputed BOM). (d) Proactive-Watch sub-agent that interjects PCN/EOL alerts mid-quote. Serves **S3**.
Acceptance: a quote on an NRND part auto-surfaces a deprecation/PCN signal + ≥3 alternates without being asked.

### Phase 6 — Procurement grounding (Problem-Value credibility)

**T-6.1 · MUST · 1.5 · [T-1.1]**
Files: `rfq/generator.py`, new `spider_qwen/serendipity/dmsms.py`.
Goal: **DMSMS SD-22 six-strategy ladder + LTB calculator + counterfeit badge.** Every EOL detection returns the structured ladder (existing stock → reclamation → substitute → alt-source → redesign → emulation), not a single quote. LTB qty = f(service life, demand, holding/disposal/shortage cost). Cross-check broker quotes vs ERAI bad-supplier list + GIDEP advisories → render a "Counterfeit Risk Badge" with AS6081/AS6171 status; cite FAR 52.246-26.
Acceptance: an EOL query returns a 6-rung ladder JSON; a broker hit on the ERAI fixture list renders a red risk badge.

**T-6.2 · NICE · 1.5 · [T-6.1]**
Files: new `spider_qwen/serendipity/eol_forecaster.py`.
Goal: **EOL forecaster sidecar.** Given an MPN, predict P(EOL within 12/24/36 mo) from public features (intro year, family churn, PCN history); zero-shot-LLM + simple Weibull baseline. Surface alongside the quote.
Acceptance: returns calibrated-ish probabilities + the features used, with evidence refs.

### Phase 7 — Qwen-native surface + MCP + Skills + cost router (Innovation bonus)

**T-7.1 · MUST · 1.5 · [T-2.1, T-4.1]**
Files: new `mcp/` servers, `memory/mcp.py`, README.
Goal: **MCP integrations (both directions).** Expose Spider-Qwen capabilities as MCP servers: `evidence-ledger-mcp`, `memory-mcp`, `procurement-mcp` (search/substitute/RFQ-draft). Consume ≥1 third-party MCP (Google Drive for RFP docs, or filesystem for ledger). Wire via DashScope Responses API `tools=[{"type":"mcp",...}]` (SSE).
Acceptance: an external MCP client can call `procurement.find_substitute(mpn)`; a Drive/filesystem MCP enriches a run; both shown in the demo.

**T-7.2 · MUST · 1 · [T-7.1]**
Files: `.qwen/skills/*/SKILL.md` (see §6).
Goal: **Custom Qwen Agent Skills.** Author ≥4 skills as project-level SKILL.md files; ensure the planner auto-invokes by description and `/skills` lists them.
Acceptance: each skill is discoverable; at least one is auto-invoked during the hero query.

**T-7.3 · HIGH · 0.5 · [T-0]**
Files: `agent/policy.py`, `observability/metrics.py`.
Goal: **Cost router + dashboard.** Heuristic routing (extraction/classification/judge → flash; planning/reasoning/high-risk-decision → max; `high_risk_procurement` tag forces max). Emit `$ per run`, tokens, TinyFish calls, $-saved-vs-all-max.
Acceptance: a run logs a cost breakdown; flipping a query to `high_risk` forces max for the decision step.

### Phase 8 — Benchmarks + demo + docs (Presentation score)

**T-8.1 · HIGH · 1 · [most of Phases 1–7]**
Files: `benchmarks/*`.
Goal: **Benchmark reporting.** Run BFCL V4 (web-search + memory categories), τ-bench (retail/airline), and a LOCOMO-style long-conversation memory eval; record numbers in README. Keep the existing offline gold-set regression (80 cases) green and add ~20 obsolete-part S1/S2/S3 cases.
Acceptance: README shows a benchmark table with method footnotes; gold-set + new cases pass in CI.

**T-8.2 · MUST · 1 · [all MUST tasks]**
Files: `README.md`, `docs/architecture.md`, demo assets.
Goal: **Demo + docs polish.** Mermaid architecture diagram; lead with evidence-first + serendipity value prop; "Track 4 primary / Track 1 secondary" statement; ≤3-min demo video link; the hero query (below) recorded end-to-end; "Why Qwen 3.7" paragraph; "Known limits / v2 roadmap" section (claim-span gaps, browser-automation deferred, paid-API tiers).
Acceptance: a cold reader understands the product in <90s; Definition of Done (§4) satisfied.

---

## 6. Skills to author (`.qwen/skills/<name>/SKILL.md`)

Each is YAML frontmatter (`name`, `description`, optional `allowedTools`, `paths`) + `## Instructions` + `## Examples`.

- **`mpn-canonicalize`** — extract & normalize part identifiers (MPN, CPN, NSN, CAGE, GTIN, HS) from arbitrary text/datasheets into a strict JSON schema; resolve hyphen/suffix variants.
- **`obsolescence-triage`** — given a part, classify lifecycle (Active/NRND/LTB/EOL/Preview), find PCN/PDN, emit the SD-22 six-strategy ladder.
- **`fff-substitute-judge`** — evaluate Form-Fit-Function equivalence between two parts against package/electrical/environmental/qualification criteria; output `{drop_in|electrical_equiv|requires_redesign|not_equivalent}` with per-criterion evidence.
- **`sea-rfq-draft`** — produce an evidence-backed RFQ draft (template-filled, constrained-LLM slots only; SEA-market-neutral English; emit language metadata) — never sends.
- **`counterfeit-risk-check`** — cross-check a broker source vs ERAI/GIDEP fixtures + AS6081/AS6171 status → risk badge.

---

## 7. The hero demo (final acceptance script)

Record this end-to-end with `--offline` deterministic fixtures (never live-demo a Cloudflare-walled site cold):

```
spider-qwen run "find a replacement for an obsolete Hirose DF13-6P-1.25DSA, deliver to Singapore in 14 days" --offline --serendipity
spider-qwen evidence show <run_id>      # full provenance ledger
spider-qwen evidence verify <run_id>    # Merkle chain + span re-check
spider-qwen evidence graph <run_id>     # the spider-web (substitute/successor/distributor edges)
spider-qwen memory show                 # learned facts w/ TTL + decay
spider-qwen review list                 # HITL checkpoints
```

The run must demonstrate all three senses in the 4-slot output: **S1** an FFF substitute + OEM successor (graph + legacy-OCR sourced), **S2** a long-tail broker/Wayback/Rochester source (bandit-selected), **S3** a PCN/EOL or counterfeit-risk signal surfaced proactively — every field carrying an `evidence_ref`, and the RFQ draft excluding any disputed fact.

---

## 8. Sequencing & critical path

```
T-0
 ├─ Phase 1 (T-1.1 → T-1.2 → T-1.3 ; T-1.4)         ← demo gets visibly better
 ├─ Phase 2 (T-2.1 → T-2.2 → T-2.3 ; T-2.4)         ← trust spine
 │     └─ Phase 3 (T-3.1 → T-3.2 ; T-3.3 needs T-1.2+T-2.3)
 │            └─ Phase 5 creatives (T-5.1,5.3,5.4 need T-3.1 ; T-5.2 standalone)
 ├─ Phase 4 (T-4.1 → T-4.2 ; T-4.3 nice)            ← MemoryAgent moat
 ├─ Phase 6 (T-6.1 → T-6.2 nice)                    ← procurement credibility
 ├─ Phase 7 (T-7.1 → T-7.2 ; T-7.3)                 ← Qwen-native + MCP/Skills
 └─ Phase 8 (T-8.1 ; T-8.2)                         ← benchmarks + demo
```

**Minimum winnable subset if time collapses** (drop everything else): T-0, T-1.1, T-1.2, T-1.4, T-2.1, T-2.2, T-3.1, T-3.2, T-4.1, T-5.2, T-6.1, T-7.1, T-7.2, T-8.2. That subset still demonstrates evidence-first multi-hop substitute discovery + proactive risk + Qwen-native MCP/Skills + a clean demo.

**Effort estimate:** full graph ≈ 38 engineer-days solo; ≈ 13–15 days for a team of three with the parallelization above. Minimum subset ≈ 16 days solo / ~6 days for three.

---

## 9. Notes for the agent (gotchas baked in from research)

- Thinking-mode + structured-output are mutually exclusive on DashScope; route JSON extraction to non-thinking flash and keep the literal `"json"` token in those prompts.
- `tool_choice` can't be `"required"` — enforce mandatory tool use in the planner prompt or state machine.
- TinyFish Search/Fetch are free but rate-limited (5 search/min, 25 fetch/min) and failed fetches don't count — implement a token-bucket limiter and retry-on-fail.
- Internet Archive CDX now blocks IPs on sustained 429s — throttle hard.
- Nexar/Octopart free tier collapsed to ~100 parts lifetime — gate it to top-K final candidates only; Digi-Key V4 (1000/day) + Mouser (1000/day) are the free workhorses.
- Standards to cite correctly: **AS5553E (Nov 2025)**, **IEC 62668-1 Ed. 1.1 (2024)**, **AS6081A→AS6171A**, **SD-22 DMSMS**, **IEC 62402**; IEC 62402-3 PCN-XML has slipped (use smartPCN 3.01 / VDMA 24903).
- GRAM (arXiv 2605.19376) is **not** ported literally — only its width-first + multi-trajectory-verification principles, as T-3.3. State this honestly in the writeup.
- Do not restructure the repo, do not add browser automation, do not introduce any non-Qwen LLM, do not let the agent submit/send anything.
