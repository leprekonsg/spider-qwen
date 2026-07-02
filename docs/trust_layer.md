# Trust layer: adopted algorithms, adaptations, and guarantees

Normative reference for the seven adapted algorithms in the trust layer. The
chronological decision log lives in `implementation-notes.html`; operations in
`README.md`. This file is the single source of truth for what each component
guarantees and what it does not.

**Drift rule:** every guarantee below names the test that pins it. A behavior
change must update the test and this file in the same commit. A claim without a
named test does not belong here.

---

## 1. Transparency log (Merkle tree)

- **Source:** RFC 6962 (generation), RFC 9162 (verification).
- **Where:** `evidence/transparency.py`, `evidence/ledger.py`.
- **Adopted:** 0x00 leaf / 0x01 node domain separation; inclusion and
  consistency proofs verified by pure functions needing no log access;
  citation proof bundles bound to the published (persisted) tree head, never a
  recomputed one; STH signatures verified only against an out-of-band anchor,
  never the key embedded in the STH.
- **Adapted:** per-run ledgers; salted leaf redaction (per-leaf HMAC-derived
  salts); `annotate()` refuses rows covered by a published commitment.
- **Guarantee:** tamper-evidence relative to a checkpoint the operator
  presents. Malformed proofs verify False, never raise.
- **Not guaranteed:** third-party verifiability or split-view detection. Those
  require the operator public key distributed out of band plus checkpoints
  published where the operator cannot rewrite (external git/TSA/witness);
  neither ships with the repo. Do not describe the log as "externally
  verifiable" without that infrastructure.
- **Config:** `SPIDER_QWEN_STH_SIGNING_KEY`; anchor precedence
  `--sth-public-key` > `SPIDER_QWEN_STH_PUBLIC_KEY` > `..._FILE` > derived
  from the signing key (operator-local convenience, i.e. self-verification).
- **Pinned by:** `test_inclusion_proof_rejects_wrong_leaf_index_and_tampered_leaf`,
  `test_consistency_rejects_forked_log`,
  `test_sth_signed_by_attacker_key_fails_against_trust_anchor`,
  `test_load_rejects_ledger_tampered_after_commitment`,
  `test_malformed_proof_elements_verify_false_not_crash`
  (tests/test_transparency_log.py).

## 2. Dempster-Shafer belief fusion with Yager fallback

- **Source:** Shafer (1976); Yager (1987); Zadeh's paradox as the failure mode.
- **Where:** `evidence/belief.py`.
- **Adopted:** BPAs over the frame {true, false}; `[Bel, Pl]` intervals;
  Dempster's rule via one n-ary conjunctive pass.
- **Adapted:** each source's BPA is reliability-discounted at construction
  (mass `r` on its verdict, `1-r` on unknown, `r` from the policy source-tier
  table) - this is Shafer discounting folded into `bpa()`, the
  literature-standard remedy for conflict. The Yager fallback engages only
  above TOTAL conjunctive conflict q(empty) > 0.8 (never max pairwise K) and
  parks the conflict mass on unknown once, after combining every source.
  Reliability clamped at 0.99 so K = 1 is unreachable.
- **Guarantee:** the fused interval is a pure function of the input multiset
  (order-independent); malformed BPAs fail loud; high multi-source conflict
  surfaces as unknown mass, not manufactured certainty.
- **Not guaranteed:** the 0.8 threshold is a safety valve, not a calibrated
  quantity. If fusion without reliability knowledge is ever needed, PCR6 is
  the defensible alternative; do not add a second combination rule without
  recording the decision here.
- **Config:** `YAGER_CONFLICT_THRESHOLD`, `UNCERTAINTY_TAU` (belief.py);
  reliability tiers from `policy_config.yaml source_reliability` via
  `ledger.reliability_priors` (memory-recall rows included).
- **Pinned by:** `test_yager_trigger_uses_total_not_max_pairwise_conflict`,
  `test_fusion_is_order_independent_for_the_same_multiset`,
  `test_multi_source_high_conflict_surfaces_unknown_not_certainty`,
  `test_malformed_bpa_fails_loud_not_silent` (tests/test_belief_fusion.py).

## 3. Claim verification gate (MiniCheck-style) + SAFE re-verification

- **Source:** MiniCheck (Tang et al., arXiv:2404.10774) as the shape of the
  gate; SAFE-style corpus re-verification; FActScore-style atomic
  decomposition (Min et al., arXiv:2305.14251).
- **Where:** `verification/minicheck.py`, `verification/safe.py`,
  `verification/atomic.py`, `evidence/verifier.py`.
- **Adapted (deliberately deterministic):** instead of a trained checker, a
  claim's concrete value must be literally present in the cited page text
  (boundary-preserving normalization: currency marks stripped, digit-group
  commas removed, whitespace collapsed but never deleted), and vendor-scoped
  relation claims (price, MOQ, quote channel, contacts) additionally require
  the value and vendor in the same sentence. Token-overlap fallback requires
  distinctive-token coverage AND overall coverage (min of the two ratios);
  values with only generic legal tokens need every token present. Relation
  claims with no concrete value fail closed (`no_value`). The optional Qwen
  NLI seam (`QWEN_NLI_ENABLED`) is clamped and re-gated: the model cannot
  bypass the co-location guard, crash the path, or verify an empty value.
- **Guarantee:** a fabricated value cannot verify against its own extraction
  snippet (grounding always runs against the parent page text); a critical
  claim that fails both the cited span and SAFE corpus re-verification blocks
  the candidate.
- **Not guaranteed:** semantic entailment. Known blind spots of the lexical
  gate: negation ("does not exceed $500"), unit/currency conversion,
  aggregation across sentences, temporal qualifiers (stale prices), and
  wrong-predicate matches beyond keyword co-location. If these matter, add
  deterministic grade-degraders (negation-cue window, unit canonicalization,
  predicate-anchor lexicon, temporal guard, hedge detection) rather than
  weakening the gate.
- **Pinned by:** `test_price_grounds_next_to_quantity_column`,
  `test_short_value_does_not_ground_across_token_boundaries`,
  `test_vendor_name_cannot_verify_on_legal_boilerplate`,
  `test_empty_value_relation_claim_fails_closed`
  (tests/test_trust_seam_fixes.py); spine behavior in
  tests/test_verification_spine.py.

## 4. Typed grounding + corrective actuation (GSAR / CRAG / CiteFix)

- **Source:** CRAG action space (Yan et al., arXiv:2401.15884); RARR's
  edit-vs-recite boundary (Gao et al., arXiv:2210.08726); CiteFix citation
  re-pointing (Maheshwari et al., arXiv:2504.15629).
- **Where:** `verification/grounding.py`, `evidence/verifier.py`,
  `agent/controller.py`.
- **Label -> actuator (the honest mapping):**

  | Label | Actuator |
  |---|---|
  | `grounded` | keep |
  | `complementary` | re-point: the corroborating row's ref joins the candidate's citations; claim row records `repointed_to` |
  | `contradicted` | one bounded CRAG replan round (rewritten pivot queries, re-verify, never a loop) |
  | `ungrounded` | no actuator; a critical ungrounded claim blocks via `verified=False` |

- **Re-pointing constraints (CiteFix/RARR):** the new span must pass the SAME
  full relation gate that the original citation failed (SAFE already applied
  it); claim text is never edited; the repair is ledger-logged.
- **Guarantee:** an emitted RFQ's citations include a span that actually
  supports each verified claim's value.
- **Not guaranteed:** multi-span joint support (a claim supported only by a
  SET of spans re-points to the single best span); numeric contradiction is
  detected only in price/quantity context and non-numeric values never
  auto-contradict (conservative by design).
- **Pinned by:** `test_complementary_claim_is_repointed_to_corroborating_row`
  (tests/test_trust_seam_fixes.py), `test_grounded_proceeds`,
  `test_contradicted_replans`,
  `test_numeric_contradiction_requires_subject_colocation`,
  `test_spine_emits_contradicted_replan_when_source_disagrees`
  (tests/test_grounding_and_grade.py).

## 5. Evidence grading (GRADE)

- **Source:** GRADE (Guyatt et al., J Clin Epi 2011), used as analogy. The key
  import is GRADE's decoupling of evidence CERTAINTY from action STRENGTH.
- **Where:** `verification/grade.py`, `agent/policy.py`, `rfq/generator.py`.
- **Adapted:** start tier derives from the policy source-reliability table
  (one table, never a second copy); downgrades for contradiction (-2),
  ungrounded (-2), complementary (-1), no exact span (-1); upgrade (+1) only
  for three corroborating spans on DISTINCT registrable hosts (the vendor's
  own pages are one source). The DS-imprecision downgrade is reserved: the v1
  spine never supplies `ds_uncertainty`.
- **Action policy (the gate half):** `rfq.grade_floor` in policy_config.yaml.
  Default `very_low` = the grade is purely advisory (no draft ever held on
  grade alone). Raised to `low`/`moderate`, drafts below the floor are held
  for human review, with the grade and floor stated. Invalid floor values
  fail loud at read time.
- **Guarantee:** the candidate grade is the worst grade among verified claims
  and fails closed to `very_low` when nothing verifies; aggregators fail
  closed on unknown strings.
- **Pinned by:** `test_start_tiers_track_source_reliability_table`,
  `test_corroboration_upgrades_grounded_claims_only`
  (tests/test_grounding_and_grade.py);
  `test_corroboration_upgrade_ignores_same_host_repeats`,
  `test_corroboration_upgrade_counts_distinct_hosts`,
  `test_grade_at_least_ordering_fails_closed`,
  `test_policy_grade_floor_defaults_advisory_and_fails_loud`
  (tests/test_trust_seam_fixes.py).

## 6. Citation-credit recall ranking (RMM-style, spec item W1)

- **Where:** `memory/citation_rank.py`, `agent/controller.py`,
  `memory/recall.py`.
- **Mechanism:** recall boost `1 + 0.2 * log2(1 + citation_count)`: 1.0 at
  zero citations, logarithmic so a runaway favorite cannot drown fresh facts.
- **Adapted:** the spec's freshness term is dropped (Ebbinghaus decay already
  prices it; double-counting punishes old-but-reliable facts twice) and chain
  position is dropped (per-run ledgers carry no cross-run position signal).
- **Closed-loop breaker (the load-bearing constraint):** credit requires the
  verification spine. A recalled fact is creditable only when the candidate
  citing it VERIFIED, and recalled rows carry no premise text, so they must
  re-ground through SAFE against the CURRENT run's fetched corpus. Without
  this, a recalled fact that itself made the candidate validate would
  self-reinforce with no external check.
- **Pinned by:** `test_multiplier_is_one_at_zero_and_logarithmic`,
  `test_recall_ranks_cited_fact_above_equal_uncited_fact`
  (tests/test_citation_rank.py); the end-to-end loop in
  tests/test_trust_dataflow.py.

## 7. Statistical emission gate (LTT selective risk) + coverage advisory

- **Source:** Learn-then-Test (Angelopoulos et al., arXiv:2110.01052);
  selective-risk target as in SGR (Geifman & El-Yaniv, arXiv:1705.08500);
  split conformal (Angelopoulos & Bates, arXiv:2107.07511) for the advisory.
- **Where:** `verification/conformal.py`, `agent/controller.py`,
  `api/cli.py` (`spider-qwen calibrate template|check`).
- **Mechanism (the gate):** exact binomial tail p-values
  `BinomCDF(k; m, alpha)` over the fixed threshold grid (0.5, 0.75, 0.9)
  under Bonferroni at `delta/3`; the smallest certified threshold deploys.
  The gate reads the CRITICAL-claim verifier score only. A fixed grid is
  deliberate: a fixed-sequence walk from the strictest threshold has one
  emitted example at the top and can never reject; from the loosest it dies
  on its first failure.
- **Guarantee (state it exactly this way):** with probability >= 1-delta over
  the calibration draw, the long-run fraction of emitted candidates that are
  wrong is at most alpha - provided future candidates are exchangeable with
  the calibration candidates (same pipeline version, same query mix) and
  "wrong" means what the hand-grading measured. Marginal, not per-candidate;
  not per-mode unless calibrated per mode; void on any pipeline-version
  change (prompts, models, search provider, ranking, policy).
- **Not guaranteed:** anything, when uncalibrated - the gate then never
  blocks and states the missing prerequisite. Labels produced by the verifier
  being gated make the guarantee circular ("disagrees with labeler", not
  "wrong"); grading stays human.
- **Floors:** zero-error certification needs
  `ceil(ln(delta/3) / ln(1-alpha))` emitted calibration examples: 33 at
  alpha=delta=0.1, 84 at 0.05/0.05. Undersized calibration is useless, not
  unsafe.
- **Coverage advisory:** the split-conformal `ConformalAbstainer` (calibrated
  on correct examples, threshold at the `ceil((n+1)(1-alpha))` quantile of
  nonconformity) bounds false abstention on CORRECT predictions at alpha. It
  is a recall diagnostic. It must never gate emission: it places no bound on
  confident-but-wrong candidates.
- **Config:** `SPIDER_QWEN_CONFORMAL_CALIBRATION` -> JSON
  `{"alpha": 0.1, "delta": 0.1, "examples": [{"verifier_score": 0.9,
  "prediction_correct": true}, ...]}`. Malformed files fail loud at
  controller construction. Metrics: `RunResult.metrics.conformal`
  (`risk_bound`, `confidence`, `candidates_abstained`).
- **Pinned by:** `test_selective_gate_refuses_below_ltt_floor`,
  `test_selective_gate_certifies_with_enough_error_free_mass`,
  `test_selective_gate_threshold_sits_above_observed_errors`
  (tests/test_trust_seam_fixes.py);
  `test_calibrated_abstention_gates_candidate_emission`,
  `test_uncalibrated_abstainer_never_gates_and_states_why`
  (tests/test_conformal.py);
  `test_check_refuses_insufficient_data_for_selective_gate`
  (tests/test_calibration_harness.py).

---

## Shared invariants

- **One source-reliability table.** `policy_config.yaml source_reliability`
  overrides flow through `EvidenceLedger.reliability_priors` into ledger
  confidence, GRADE start tiers, verifier source classes, and memory-row
  belief weights. A component reading `DEFAULT_RELIABILITY` directly without
  merging the ledger priors is a bug.
- **Trust attribution keys on vendor + registrable domain**
  (`Controller._assessment_key`), matching candidate dedupe; name-only keys
  attribute one candidate's grade to another. Pinned by
  `test_assessment_key_separates_same_name_vendors`.
- **Evidence or it didn't happen.** No trust surface (grade, interval,
  verdict, proof) is computed from anything but ledger rows, and verdict
  metadata is written back through `EvidenceLedger.annotate()` so the chain
  binds it.
