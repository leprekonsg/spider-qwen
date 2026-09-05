"""Budgeted, policy-bound controller.

Orchestrates the deterministic pipeline:
  classify -> budget -> search (SEA-first) -> fetch -> extract -> rank
  -> [RFQ draft] -> persist evidence + memory.

Qwen is the planner/controller in spirit; v1 execution is deterministic. Every
ranked output and RFQ draft references ledger evidence. RFQ drafts are never
submitted or sent.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from .budget import Budget, BudgetExceeded, BudgetTracker, StopReason
from .compiler import LLMCompiler, NullRateLimiter, RateLimiter, ToolNode
from .execution_context import ExecutionContext, new_run_id
from .frontier import (
    Frontier,
    Lead,
    apply_scorer_deltas,
    entity_query_lead,
    leads_from_search_results,
    link_leads_from_page,
    rank_leads,
    term_overlap,
)
from .planner import Planner
from .policy import Policy, load_policy
from ..api.schema import Classification, RunResult
from ..evidence.ledger import EvidenceLedger
from ..evidence.graph import render_supplier_graph
from ..evidence.models import EvidenceRef, sha256_hex, utc_now_iso
from ..evidence.verifier import VerificationSpine
from ..verification.grade import grade_at_least
from ..verification.minicheck import MiniCheck, relation_grounded, value_grounded
from ..extraction.contact import ContactExtractor
from ..extraction.dedupe import dedupe_candidates, normalize_vendor_name
from ..extraction.pricing import PricingExtractor, PricingResult
from ..extraction.quote_channel import QuoteChannelExtractor, QuoteChannelMatch
from ..extraction.service_match import ServiceMatchExtractor
from ..extraction.vendor_metadata import VendorMetadataExtractor
from ..identity import registrable_domain
from ..governance.audit import AuditLog
from ..governance.review_events import ReviewStore
from ..memory.episodic import EpisodicMemory, EpisodicRecord
from ..memory.mcp import SemanticMemoryMcpAdapter
from ..memory.promotion import should_promote_contact
from ..memory.citation_rank import record_citations
from ..memory.recall import rfq_eligible
from ..memory.semantic import MemoryRecall, SemanticFact, SemanticMemory
from ..memory.working import WorkingMemory
from ..modes.classifier import ModeClassifier
from ..modes.contracts import (
    Contact,
    ContactCandidate,
    PricingStatus,
    ProcurementMode,
    ProductCandidate,
    QuoteChannel,
    ServiceCandidate,
)
from ..modes.qwen_router import QwenModeRouter, QwenModeRouterError
from ..modes.router import ModeRouter, RoutePlan
from ..observability.metrics import CostMeter, Metrics
from ..observability.tracing import TraceEvent, Tracer
from ..ranking.contact_ranker import ContactRanker
from ..ranking.geo_strategy import SEA_COUNTRIES, GeoStrategy, build_query_templates
from ..ranking.product_ranker import ProductRanker
from ..evidence.belief import quote_channel_interval
from ..ranking.serendipity import build_serendipity_result, disputed_fact_signals
from ..ranking.service_ranker import ServiceRanker
from ..serendipity.corrective import corrective_queries, evaluate_retrieval
from ..serendipity.query_rewrite import merge_gather_queries
from ..rfq.generator import RFQGenerator
from ..tools.fetch_service import FetchService, build_fetch_provider
from ..tools.page_judge import PageJudge
from ..tools.qwen_json_extractor import QwenJsonExtractor, QwenPageExtraction
from ..tools.search_service import SearchService, build_search_provider

_MOQ_RE = re.compile(r"(?:MOQ|minimum order(?: quantity)?)\D{0,15}([\d,]+)", re.IGNORECASE)
# Qwen page roles that never become candidates: buyer-side notices, awards,
# news, and multi-vendor directories. Directory pages still contribute leads
# (link leads + vendor mentions); they just stop masquerading as vendors.
_NON_VENDOR_PAGE_ROLES = {"buyer_rfq", "tender_award", "news", "directory"}
_VENDOR_MENTIONS_PER_PAGE = 10
# Budget held back in early frontier rounds so entity follow-up leads (vendor
# mentions on gated pages, ungrounded quote channels) can still be searched and
# fetched after the SERP leads of round 1; released once no entity queries are
# pending. Without the reserve, round 1 spends everything and mention leads die
# in the queue (live finding: recall capped at 0.407 with 16 named vendors
# stranded on correctly-gated pages).
_ENTITY_FETCH_RESERVE = 3
_ENTITY_SEARCH_RESERVE = 2
# Marketplace tag/catalog/search listings are directory pages by construction;
# gate them deterministically (one Lazada tag page slipped the Qwen gate in the
# live sample). Product detail pages on these hosts are NOT matched.
_MARKETPLACE_HOST_RE = re.compile(
    r"(^|\.)(lazada|shopee|carousell|qoo10|aliexpress|alibaba|amazon|ebay)\.", re.IGNORECASE
)
_MARKETPLACE_LISTING_PATH_RE = re.compile(r"^/(tag|catalog|search)(/|$)", re.IGNORECASE)


def _marketplace_listing_page(url: str | None) -> bool:
    if not url:
        return False
    parts = urlparse(url)
    if not _MARKETPLACE_HOST_RE.search(parts.netloc or ""):
        return False
    if _MARKETPLACE_LISTING_PATH_RE.search(parts.path or ""):
        return True
    return bool(re.search(r"(^|&)q=", parts.query or ""))
_EVIDENCE_SOURCE_TOOLS = {
    "tinyfish_search", "tinyfish_fetch", "qwen_web_extractor", "mcp_search", "semantic_memory", "mock"
}
_PRICED_STATUSES = {
    PricingStatus.EXACT_PRICE,
    PricingStatus.PRICE_RANGE,
    PricingStatus.STARTING_FROM,
    PricingStatus.RATE_CARD_FOUND,
}


def _registrable(url: str | None) -> str:
    """Compatibility wrapper while controller callers migrate to shared identity."""
    return registrable_domain(url)


class Controller:
    def __init__(
        self,
        policy: Policy | None = None,
        *,
        search_provider: object | None = None,
        fetch_provider: object | None = None,
        qwen_json_extractor: object | None = None,
        page_judge: object | None = None,
        verify: bool | None = None,
        minicheck: object | None = None,
        conformal: object | None = None,
        qwen_router: object | None = None,
        memory_mcp: SemanticMemoryMcpAdapter | None = None,
        state_dir: str | Path | None = None,
        persist: bool = True,
        require_review: bool | None = None,
        offline: bool = False,
        trace_callback: Callable[[TraceEvent], None] | None = None,
    ) -> None:
        self.policy = policy or load_policy()
        self.trace_callback = trace_callback
        # offline=True is a guarantee, not a hint: NO live client is ever
        # constructed here -- search/fetch providers included -- even when
        # policy/env flags enable one and an API key is present. Injected
        # providers/scorers are still honored.
        self.offline = bool(offline)
        if self.offline:
            from ..tools.fetch_service import MockFetchProvider
            from ..tools.search_service import MockSearchProvider

            self.search_provider = search_provider or MockSearchProvider()
            self.fetch_provider = fetch_provider or MockFetchProvider()
        else:
            self.search_provider = search_provider or build_search_provider()
            self.fetch_provider = fetch_provider or build_fetch_provider()
        # Finding-6 fetch fallback (flagged): transport_error / js_shell URLs
        # get one retry through the Qwen web_extractor. Never offline, never
        # stacked on top of an already-qwen fetch provider.
        self.fetch_fallback = None
        if (not self.offline and self.policy.qwen_fetch_fallback_enabled()
                and getattr(self.fetch_provider, "provider_name", "") != "qwen_web_extractor"):
            from ..tools.qwen_web_extractor import QwenWebExtractorFetchProvider

            fallback = QwenWebExtractorFetchProvider()
            if fallback.extractor.is_available:
                self.fetch_fallback = fallback
        self.state_dir = Path(state_dir) if state_dir else None
        self.persist = persist and self.state_dir is not None
        self.require_review = self.policy.hitl_require_review() if require_review is None else require_review
        self.classifier = ModeClassifier()
        # Fail loud at init, not mid-run, when a live Qwen path is enabled with
        # a model id missing from the pinned pricing: block.
        if not self.offline and (
            self.policy.qwen_router_fallback_enabled()
            or self.policy.qwen_structured_extraction_enabled()
            or self.policy.qwen_nli_enabled()
            or self.policy.qwen_query_rewriter_enabled()
            or self.policy.qwen_rfq_drafter_enabled()
            or self.policy.qwen_frontier_scorer_enabled()
        ):
            self.policy.validate_model_ids()
        self.qwen_router = qwen_router
        if self.qwen_router is None and self.policy.qwen_router_fallback_enabled() and not self.offline:
            self.qwen_router = QwenModeRouter(model=self.policy.qwen_router_model())
        self.qwen_json_extractor = qwen_json_extractor
        if self.qwen_json_extractor is None and self.policy.qwen_structured_extraction_enabled():
            if self.offline:
                from ..tools.qwen_json_extractor import MockQwenJsonExtractor

                self.qwen_json_extractor = MockQwenJsonExtractor()
            else:
                self.qwen_json_extractor = QwenJsonExtractor(model=self.policy.qwen_json_extractor_model())
        # T-2.1: page judge gate. Opt-in (off by default) so the offline pipeline
        # is unchanged unless a judge is injected or the policy flag enables it.
        self.page_judge = page_judge
        if self.page_judge is None and self.policy.qwen_page_judge_enabled():
            self.page_judge = PageJudge()
        # T-2.2: verification spine. Opt-in (off by default) so the offline
        # pipeline is unchanged unless enabled here or via policy.
        self.verify_claims = self.policy.verification_enabled() if verify is None else verify
        self.minicheck = minicheck
        if self.minicheck is None and self.policy.qwen_nli_enabled() and not self.offline:
            # Qwen scores (claim, span) entailment through MiniCheck's model
            # seam; the seam clamps the score, re-applies the co-location
            # guard, and falls back to the heuristic on any model failure.
            # Offline: stays None -> the spine uses the deterministic heuristic.
            from ..verification.qwen_nli import QwenNliScorer

            self.minicheck = MiniCheck(model=QwenNliScorer(model=self.policy.qwen_nli_model()))
        # Statistical emission gate over verifier scores: LTT selective risk,
        # bounding P(wrong | emitted) <= alpha with confidence 1-delta -- the
        # risk an emission gate must control. Calibrated (via env calibration
        # file or injection) -> abstentions block candidates. Uncalibrated ->
        # never gates; its "guarantee unavailable" rationale is surfaced in run
        # metrics instead of fabricating a claim. (The split-conformal coverage
        # abstainer bounds the OPPOSITE risk -- false abstention on correct
        # predictions -- and is advisory only; see verification/conformal.py.)
        self.conformal = conformal
        if self.conformal is None and self.verify_claims:
            from ..verification.conformal import gate_from_env

            self.conformal = gate_from_env()
        # CRAG corrective rewriting: Qwen proposes pivot queries when retrieval
        # is judged off-target; everything downstream stays deterministic.
        self.qwen_query_rewriter = None
        if self.policy.qwen_query_rewriter_enabled():
            if self.offline:
                from ..serendipity.qwen_rewriter import MockQwenQueryRewriter

                self.qwen_query_rewriter = MockQwenQueryRewriter()
            else:
                from ..serendipity.qwen_rewriter import QwenQueryRewriter

                self.qwen_query_rewriter = QwenQueryRewriter(
                    model=self.policy.qwen_query_rewriter_model())
        # CoVe-split RFQ drafting: Qwen writes the body, a deterministic
        # fact-check flags unsourced numeric claims against ledger evidence.
        self.qwen_rfq_drafter = None
        if self.policy.qwen_rfq_drafter_enabled():
            if self.offline:
                from ..rfq.qwen_drafter import MockQwenRfqDrafter

                self.qwen_rfq_drafter = MockQwenRfqDrafter()
            else:
                from ..rfq.qwen_drafter import QwenRfqDrafter

                self.qwen_rfq_drafter = QwenRfqDrafter(
                    model=self.policy.qwen_rfq_drafter_model())
        # Frontier gather (flagged): score-before-fetch priority queue with
        # 1-hop link insertion. The linear gather path stays the default.
        self.frontier_enabled = self.policy.frontier_enabled()
        # Frontier re-scoring seam: Qwen proposes clamped per-lead deltas;
        # the deterministic frontier keeps admission authority.
        self.qwen_frontier_scorer = None
        if self.policy.qwen_frontier_scorer_enabled():
            if self.offline:
                from .qwen_frontier_scorer import MockQwenFrontierScorer

                self.qwen_frontier_scorer = MockQwenFrontierScorer()
            else:
                from .qwen_frontier_scorer import QwenFrontierScorer

                self.qwen_frontier_scorer = QwenFrontierScorer(
                    model=self.policy.qwen_frontier_scorer_model())
        # Cross-run read-through page cache (flagged, needs a state dir): hits
        # skip the provider call and consume no fetch budget; pages are still
        # judged and re-recorded in each run's own ledger.
        self.page_cache = None
        if self.state_dir is not None and self.policy.page_cache_enabled():
            from ..tools.page_cache import PageCache

            self.page_cache = PageCache(
                self.state_dir, ttl_seconds=self.policy.page_cache_ttl_seconds())
        self.memory_mcp = memory_mcp
        if self.memory_mcp is None and self.state_dir is not None:
            # ONE SemanticMemory instance serves recall, promotion, and citation
            # crediting. Two instances over the same semantic.json clobber each
            # other across runs: whichever persists last rewrites the file from
            # its own (possibly stale) in-memory dict, silently dropping the
            # other's promotions and citation counts.
            self.memory_mcp = SemanticMemoryMcpAdapter(
                self.state_dir,
                memory=SemanticMemory(
                    self.state_dir,
                    require_evidence=self.policy.semantic_promotion_requires_evidence,
                ),
            )
        self.router = ModeRouter()
        self.planner = Planner()
        # T-1.4: LLM-Compiler + free-tier token buckets (5 search/min, 25 fetch/min).
        # Throttle only when a provider hits a live external quota; offline/mock
        # providers bypass it (else the 80-case offline benchmark blocks ~1h on
        # wall-clock token refills).
        self.rate_limiter = self._build_rate_limiter()
        self.compiler = LLMCompiler(self.rate_limiter)
        self.geo = GeoStrategy(self.policy.boost_countries, self.policy.default_region)
        self._extractors = {
            "vendor_metadata": VendorMetadataExtractor(),
            "pricing": PricingExtractor(),
            "contact": ContactExtractor(),
            "quote_channel": QuoteChannelExtractor(),
            "service_match": ServiceMatchExtractor(),
        }
        self._rankers = {"product": ProductRanker(), "service": ServiceRanker(), "contact": ContactRanker()}

    def _build_rate_limiter(self) -> RateLimiter | NullRateLimiter:
        # A provider needs throttling only if it draws on a live external quota.
        # Unknown providers default to True so the live API is always protected.
        live = any(
            getattr(p, "rate_limited", True)
            for p in (self.search_provider, self.fetch_provider)
        )
        return RateLimiter() if live else NullRateLimiter()

    def graph_retrieve(self, query: str, ledger, *, top_k: int = 5):
        """T-3.2: build the supplier-part graph from a run's ledger page text and
        run PPR multi-hop retrieval. Opt-in (not in the default pipeline); every
        edge references the asserting page's ledger_id + its source reliability."""
        from ..graph.extract import ingest_text
        from ..graph.retrieve import GraphRetriever
        from ..graph.store import GraphStore

        store = GraphStore()
        for item in ledger.items():
            if item.text:
                ingest_text(store, item.text, evidence_claim_id=item.ledger_id,
                            reliability=item.reliability, valid_from=item.retrieved_at)
        return GraphRetriever(store).retrieve(query, top_k=top_k)

    def _classify(self, query: str, forced_mode: str | None = None):
        result = self.classifier.classify(query, forced_mode=forced_mode)
        if forced_mode and forced_mode != "auto":
            return result
        if result.confidence >= self.policy.qwen_router_confidence_threshold():
            return result
        if self.qwen_router is None or not getattr(self.qwen_router, "is_available", True):
            result.rationale = f"{result.rationale}; qwen router fallback unavailable"
            return result
        try:
            routed = self.qwen_router.classify(query)
        except QwenModeRouterError as exc:
            result.rationale = f"{result.rationale}; qwen router fallback failed: {exc}"
            return result
        routed.signals = {**result.signals, **routed.signals}
        routed.rationale = f"{routed.rationale}; deterministic precheck was {result.mode.value} at {result.confidence:.2f}"
        return routed

    async def run(self, query: str, mode: str = "auto", target_country: str | None = None,
                  high_risk: bool = False, serendipity: bool = False,
                  run_id: str | None = None) -> RunResult:
        phase_start = time.perf_counter()
        classification = self._classify(query, forced_mode=mode)
        chosen = classification.mode
        route = self.router.route(chosen)
        budget = self.policy.budget_for(chosen, route.budget_key)
        run_reference_ts = utc_now_iso()

        run_id = run_id or new_run_id()
        ledger = EvidenceLedger(run_id, self.state_dir,
                                reliability_priors=self.policy.source_reliability())
        tracker = BudgetTracker(budget)
        working = WorkingMemory(run_id=run_id, query=query, mode=chosen.value)
        tracer = Tracer(run_id, chosen.value, self.state_dir, on_record=self.trace_callback)
        audit = AuditLog(run_id, self.state_dir)
        review_store = ReviewStore(self.state_dir) if self.persist and self.policy.hitl_enabled() else None
        metrics = Metrics()
        ctx = ExecutionContext(
            run_id=run_id, query=query, mode=chosen, ledger=ledger,
            tracker=tracker, working=working, tracer=tracer,
        )

        search = SearchService(self.search_provider, ledger, tracker, tracer)
        fetch = FetchService(self.fetch_provider, ledger, tracker, tracer,
                             judge=self.page_judge, query=query, cache=self.page_cache,
                             fallback=self.fetch_fallback)
        memory_recalls = self._recall_memory(query, ctx, audit, reference_ts=run_reference_ts)

        if review_store and mode == "auto" and classification.confidence < self.policy.qwen_router_confidence_threshold():
            review_store.create(
                run_id=run_id,
                reason="low-confidence classification",
                proposed_action=f"use mode {chosen.value}",
                detail=classification.model_dump(mode="json"),
            )

        if target_country is None:
            target_country = self._detect_target_country(query)

        # SEA-first gather, then global fallback only if min not met.
        sea_pages: list = []
        initial_queries: list[str] = []
        candidates = await self._gather(
            ctx, route, query, search, fetch, region="SEA", target_country=target_country,
            reserve_search_calls=1 if budget.max_search_calls > 1 else 0, pages_out=sea_pages,
            queries_out=initial_queries,
        )
        candidates, sea_merges = dedupe_candidates(candidates)
        self._record_consolidation(tracer, len(candidates) + sea_merges, candidates, sea_merges)
        candidates = self._apply_memory_recalls(ctx, candidates, memory_recalls)
        candidates = self._apply_consolidation_safety(candidates)
        ranker = self._rankers[route.ranker]
        ranked = ranker.rank(candidates)
        validated = [c for c in ranked if self._is_validated(c, chosen, budget)]

        # T-1.3: CRAG corrective evaluation of the SEA retrieval quality.
        crag = evaluate_retrieval(query, sea_pages)
        tracer.record(
            step="crag_evaluate", tool="qwen_corrective", status="success",
            input_count=len(crag.assessments),
            detail={"verdict": crag.verdict, "confidence": crag.confidence,
                    "mean_relevance": crag.mean_relevance, "pages": len(crag.assessments)},
        )
        corrective_searches = 0
        corrective_query_log: list[dict[str, str]] = []

        extraction_budget_remaining = tracker.candidates_extracted < budget.max_candidates_to_extract
        if (
            len(validated) < budget.min_validated_candidates
            and extraction_budget_remaining
            and tracker.can_search()
            and not tracker.runtime_exceeded()
        ):
            if crag.verdict == "incorrect" and crag.assessments:
                # Retrieval judged off-target: broaden / broker-pivot rather than answer.
                corr = corrective_queries(query, crag, mode=chosen.value,
                                          llm=self.qwen_query_rewriter)
                corrective_query_log = [
                    {"text": c.text, "kind": c.kind, "rationale": c.rationale} for c in corr
                ]
                tracer.record(step="crag_corrective", tool="search", status="success",
                              detail={"verdict": crag.verdict, "queries": [c.text for c in corr]})
                before = tracker.search_calls
                more = await self._gather_queries(
                    ctx, route, [c.text for c in corr], search, fetch,
                    location=None, target_country=target_country, pages_out=sea_pages,
                )
                corrective_searches = tracker.search_calls - before
            else:
                tracer.record(step="geo_fallback", tool="search", status="success")
                more = await self._gather(
                    ctx, route, query, search, fetch, region="global", target_country=target_country
                )
            candidates, fallback_merges = dedupe_candidates(candidates + more)
            self._record_consolidation(
                tracer, len(candidates) + fallback_merges, candidates, fallback_merges,
            )
            candidates = self._apply_memory_recalls(ctx, candidates, memory_recalls)
            candidates = self._apply_consolidation_safety(candidates)
            ranked = ranker.rank(candidates)
            validated = [c for c in ranked if self._is_validated(c, chosen, budget)]

        # Keep the complete already-fetched qualification pool.  Verification
        # starts with the presentation cap, then can refill from this reserve
        # before any paid corrective search is considered.
        qualified_candidates = list(validated)
        validated = qualified_candidates[: budget.max_validated_candidates]
        gather_done = time.perf_counter()

        # T-2.2: verification spine. Block candidates whose critical claims are not
        # entailed by their cited evidence; write verified/verifier_score onto the
        # claim ledger rows. Opt-in, so the default offline pipeline is unchanged.
        verification_metrics = {"claims_verified": 0, "claims_unsupported": 0,
                                 "candidates_blocked_unverified": 0,
                                 "verification_assessments": {},
                                 "replan_recommended": False,
                                 "reserve_candidates_verified": 0}
        replan_queries: list[str] = []
        if self.verify_claims:
            validated, verification_metrics = self._verify_with_reserve(
                ledger, qualified_candidates, tracer, budget,
            )
            # Bounded replan: the spine's worst GSAR decision plays the CRAG
            # retrieval evaluator (Yan et al. 2024) -- "replan" means the cited
            # corpus cannot ground the critical claims, so re-retrieve once
            # with rewritten pivot queries and re-verify. Exactly one round,
            # only within budget; never a loop.
            if (
                verification_metrics.get("replan_recommended")
                and len(validated) < budget.min_validated_candidates
                and tracker.candidates_extracted < budget.max_candidates_to_extract
                and tracker.can_search()
                and not tracker.runtime_exceeded()
            ):
                corr = corrective_queries(query, crag, mode=chosen.value,
                                          llm=self.qwen_query_rewriter)
                replan_queries = [c.text for c in corr]
                tracer.record(step="verification_replan", tool="search", status="success",
                              detail={"queries": replan_queries})
                more = await self._gather_queries(
                    ctx, route, replan_queries, search, fetch,
                    location=None, target_country=target_country, pages_out=sea_pages,
                )
                candidates, replan_merges = dedupe_candidates(candidates + more)
                self._record_consolidation(
                    tracer, len(candidates) + replan_merges, candidates, replan_merges,
                )
                candidates = self._apply_memory_recalls(ctx, candidates, memory_recalls)
                candidates = self._apply_consolidation_safety(candidates)
                ranked = ranker.rank(candidates)
                qualified_candidates = [
                    c for c in ranked if self._is_validated(c, chosen, budget)
                ]
                pre_replan = verification_metrics
                validated, verification_metrics = self._verify_with_reserve(
                    ledger, qualified_candidates, tracer, budget,
                )
                verification_metrics["replan_rounds"] = 1
                # The re-verify must not erase round-1 outcomes from RunResult
                # metrics: candidates blocked or abstained before the replan
                # would otherwise vanish from the record (trace-only).
                verification_metrics["pre_replan"] = {
                    "claims_verified": pre_replan.get("claims_verified", 0),
                    "claims_unsupported": pre_replan.get("claims_unsupported", 0),
                    "candidates_blocked_unverified": pre_replan.get(
                        "candidates_blocked_unverified", 0),
                    "candidates_abstained": (pre_replan.get("conformal") or {}).get(
                        "candidates_abstained"),
                }
        verification_metrics.setdefault("replan_rounds", 0)
        verify_done = time.perf_counter()

        # Synthesis A: ledger-supervised citation counts. A VERIFIED candidate
        # whose evidence chain includes a recalled semantic_memory row actually
        # *used* that fact, so its citation_count grows and the recall ranker
        # boosts it next session (memory/citation_rank.py). Crediting requires
        # the verification spine: without it, a recalled fact that itself made
        # the candidate validate (quote_channel fill -> completeness 0.75)
        # would self-reinforce with no external check -- a closed loop. The
        # spine breaks the loop by forcing recalled rows through SAFE corpus
        # re-grounding against the CURRENT run's fetched pages.
        if memory_recalls and self.memory_mcp is not None and hasattr(self.memory_mcp, "memory"):
            creditable = validated if self.verify_claims else self._memory_credit_verified_candidates(
                ledger, validated, tracer,
            )
            cited = record_citations(self.memory_mcp.memory, ledger, creditable)
            if cited:
                audit.record(
                    "memory_citations_recorded",
                    count=cited,
                    verifier="global_spine" if self.verify_claims else "memory_credit_spine",
                )
            elif self._has_semantic_memory_refs(ledger, validated):
                audit.record("memory_citations_skipped",
                             reason="no recalled semantic-memory claim passed verification")

        stop_reason = self._stop_reason(chosen, validated, candidates, tracker, budget)

        # T-1.1: reshape the ranked candidates into the four-slot serendipity view.
        # Disputed memory facts about this run's vendors whose fused [Bel, Pl]
        # gap exceeds UNCERTAINTY_TAU surface as explicit S3 risk signals.
        # A contradictory consolidated claim stays in the evidence ledger and
        # candidate diagnostics, but cannot become the serendipity primary
        # answer while it is withheld from validated output.
        ranked_for_surface = [
            candidate for candidate in ranked
            if not self._has_finalization_conflict(candidate)
        ]
        disputed_signals = self._disputed_belief_signals(ledger, ranked_for_surface, audit)
        serendipity_result = build_serendipity_result(
            ranked_for_surface, mode=chosen.value,
            extra_risk_signals=disputed_signals,
        )

        rfq_start = time.perf_counter()
        rfq_drafts: list[dict] = []
        if route.produces_rfq:
            rfq_drafts = self._build_rfqs(
                query, validated, target_country, metrics, audit, run_id, review_store,
                ledger=ledger,
                assessments=verification_metrics["verification_assessments"],
            )
        rfq_done = time.perf_counter()

        metrics.search_calls_total = tracker.search_calls
        metrics.fetch_urls_total = tracker.fetch_urls
        metrics.validated_candidates_total = len(validated)
        metrics.candidates_considered = len(candidates)
        metrics.quote_channel_found = sum(
            1 for c in candidates if isinstance(c, ServiceCandidate) and c.quote_channel is not None
        )
        metrics.avg_runtime_seconds = round(tracker.elapsed_seconds(), 3)
        metrics.budget_exhausted = tracker.stop_reason is not None

        # T-7.3 cost dashboard. The offline pipeline calls no model, so the meter
        # is empty (zero $); the report still logs TinyFish calls + the routing
        # plan (decision -> max under the high_risk_procurement tag).
        cost_meter = CostMeter()
        # Live token metering: drain per-call usage accumulated by Qwen clients
        # during this run. Offline mocks record nothing, so the report stays
        # honestly "token metering unavailable". Drain semantics keep a
        # long-lived controller from double-counting across runs.
        for client in (self.qwen_json_extractor, self.qwen_router,
                       getattr(self.minicheck, "model", None),
                       self.qwen_query_rewriter, self.qwen_rfq_drafter,
                       self.qwen_frontier_scorer):
            drain = getattr(client, "drain_usage", None)
            if callable(drain):
                for model, in_tok, out_tok in drain():
                    cost_meter.record(model, input_tokens=in_tok, output_tokens=out_tok)
        routing = [self.policy.route_task(step, high_risk=high_risk)
                   for step in ("classification", "planning", "extraction", "judge", "decision")]
        max_model = next((r.model for r in routing if r.tier == "max"), "")
        metrics.cost = cost_meter.report(
            self.policy.model_pricing(),
            max_model=max_model,
            tinyfish_calls=tracker.search_calls + tracker.fetch_urls,
            routing=routing,
        )

        # T-8.2: opt-in discovery sidecar. Runs after the pipeline on the existing
        # ledger (no new fetch/search budget), so it cannot starve verification.
        discovery = None
        if serendipity:
            from ..serendipity.discovery import build_discovery

            discovery = build_discovery(query, ledger, mode=chosen.value)

        result = RunResult(
            run_id=run_id,
            query=query,
            mode=chosen.value,
            stop_reason=stop_reason.value,
            classification=Classification(
                mode=chosen.value, confidence=classification.confidence, rationale=classification.rationale
            ),
            validated_candidates=[self._public_candidate_dump(c) for c in validated],
            withheld_candidates=[
                self._public_withheld_candidate_dump(c)
                for c in candidates if self._has_finalization_conflict(c)
            ],
            trust_verdicts=self._build_trust_verdicts(
                validated, verification_metrics, ledger, disputed_signals,
            ),
            qwen_paths=self._qwen_paths_block(
                classification, ctx.metadata.get("qwen_json_extractions", 0),
            ),
            reasoning={
                "initial_queries": initial_queries,
                "crag": {"verdict": crag.verdict, "confidence": crag.confidence,
                         "mean_relevance": crag.mean_relevance, "rationale": crag.rationale},
                "corrective_queries": corrective_query_log,
                "replan_queries": replan_queries,
                "query_rewriter": (
                    f"qwen:{getattr(self.qwen_query_rewriter, 'model', '')}"
                    if self.qwen_query_rewriter is not None else "deterministic"
                ),
            },
            serendipity=serendipity_result.model_dump(mode="json"),
            serendipity_discovery=discovery.model_dump(mode="json") if discovery else None,
            pricing_status_summary=self._pricing_summary(candidates),
            rfq_drafts=rfq_drafts,
            evidence_refs=[c_ref for c in validated for c_ref in c.evidence_refs],
            metrics={
                **metrics.model_dump(),
                "quote_channel_found_rate": metrics.quote_channel_found_rate,
                "memory_recalls": len(memory_recalls),
                "pending_reviews": len(review_store.list(status="pending")) if review_store else 0,
                "pending_review_events": [
                    {"event_id": e.event_id, "reason": e.reason,
                     "proposed_action": e.proposed_action}
                    for e in (review_store.list(status="pending") if review_store else [])
                    if e.run_id == run_id
                ],
                "crag_verdict": crag.verdict,
                "crag_confidence": crag.confidence,
                "frontier": ctx.metadata.get("frontier", {"enabled": False}),
                "corrective_searches": corrective_searches,
                "pages_rejected": fetch.rejected,
                "pages_flagged": fetch.flagged,
                # Live-web failure taxonomy: a starved run reports WHY it
                # starved (bot walls vs JS shells vs thin/dead pages).
                "fetch_outcomes": dict(sorted(fetch.fetch_outcomes.items())),
                "fetch_fallback_recovered": fetch.fallback_recovered,
                "page_cache": {
                    "enabled": self.page_cache is not None,
                    "hits": fetch.cache_hits,
                    "misses": fetch.cache_misses,
                },
                # Where a run's wall clock went: gather covers classify ->
                # search/fetch/extract/rank, verify covers the spine plus its
                # bounded replan, rfq covers drafting + fact-check.
                "latency_seconds": {
                    "gather": round(gather_done - phase_start, 3),
                    "verify": round(verify_done - gather_done, 3),
                    "rfq": round(rfq_done - rfq_start, 3),
                    "total": round(time.perf_counter() - phase_start, 3),
                },
                **verification_metrics,
            },
            budget=tracker.snapshot(),
        )

        readiness_by_id = {v["supplier_id"]: v["readiness"] for v in result.trust_verdicts}
        for candidate in result.validated_candidates:
            candidate["readiness"] = readiness_by_id[candidate["supplier_id"]]
        for candidate in result.withheld_candidates:
            candidate["readiness"] = {
                "stage": "discovered", "reasons": ["unresolved_conflicts"], "approval": "not_recorded",
            }
        self._persist_run(ctx, audit, result, validated, review_store)
        return result

    async def run_reasoning(self, query: str, mode: str = "auto", target_country: str | None = None):
        """T-R.2: GRAM-lite multi-trajectory run with PPRM winner selection.

        Opt-in alternative to ``run``: explores several strategy trajectories within
        the frozen reasoning budget, repairs evidence gaps in a bounded round 2,
        scores each bundle with the deterministic Process Reward Model, and returns
        the winning bundle plus a why-it-won / why-alternates-lost explanation. Every
        bundle ties its strategy/round to concrete ledger evidence ids (provenance).
        Deterministic and network-free under offline mock providers.
        """
        from ..reasoning.trajectory import ReasoningBudget, ReasoningTrajectory, TrajectoryBundle
        from ..reasoning.trajectory_runner import TrajectoryRunner

        classification = self._classify(query, forced_mode=mode)
        chosen = classification.mode
        route = self.router.route(chosen)
        run_id = new_run_id()
        ledger = EvidenceLedger(run_id, self.state_dir, reliability_priors=self.policy.source_reliability())
        tracer = Tracer(run_id, chosen.value, self.state_dir, on_record=self.trace_callback)
        if target_country is None:
            target_country = self._detect_target_country(query)
        rbudget = ReasoningBudget()
        ranker = self._rankers[route.ranker]

        async def executor(traj: ReasoningTrajectory) -> TrajectoryBundle:
            per_budget = Budget(
                mode=chosen.value,
                max_search_calls=rbudget.max_search_calls_per_trajectory,
                max_fetch_urls=rbudget.max_fetch_urls_per_trajectory,
                max_candidates_to_extract=rbudget.max_fetch_urls_per_trajectory,
                min_validated_candidates=1,
            )
            tracker = BudgetTracker(per_budget)
            working = WorkingMemory(run_id=run_id, query=query, mode=chosen.value)
            ctx = ExecutionContext(run_id=run_id, query=query, mode=chosen, ledger=ledger,
                                   tracker=tracker, working=working, tracer=tracer)
            search = SearchService(self.search_provider, ledger, tracker, tracer)
            fetch = FetchService(self.fetch_provider, ledger, tracker, tracer,
                                 judge=self.page_judge, query=query, cache=self.page_cache,
                                 fallback=self.fetch_fallback)
            cands = await self._gather_queries(
                ctx, route, traj.queries, search, fetch, location=None, target_country=target_country,
            )
            cands, trajectory_merges = dedupe_candidates(cands)
            self._record_consolidation(tracer, len(cands) + trajectory_merges, cands, trajectory_merges)
            cands = self._apply_consolidation_safety(cands)
            ranked = ranker.rank(cands)
            metrics, refs, disputed, conflict, qualified_count = self._bundle_metrics(
                chosen.value, ranked or cands
            )
            tracer.record(
                step="reasoning_trajectory", tool="search", status="success",
                detail={"trajectory_id": traj.trajectory_id, "strategy": traj.strategy.value,
                        "round": traj.round, "queries": traj.queries,
                        "evidence_refs": [r.ledger_id for r in refs], "parent_run_id": run_id},
            )
            return TrajectoryBundle(
                trajectory=traj, metrics=metrics, evidence_refs=refs, candidate_count=len(cands),
                disputed_count=disputed, searches_used=tracker.search_calls,
                fetches_used=tracker.fetch_urls, conflict_penalty=conflict,
                qualified_candidate_count=qualified_count,
            )

        return await TrajectoryRunner(budget=rbudget).run(query, chosen.value, executor=executor)

    def _bundle_metrics(self, mode: str, candidates: list):
        """Aggregate metrics from individually qualifying suppliers only.

        The previous implementation took independent maxima across every candidate.
        That could score a trajectory as if one supplier had another supplier's
        service fit, quote channel, and geography.  Each vector below belongs to
        one candidate; dimensions are then averaged across candidates that meet
        the mode's minimum qualification condition.  Coverage rewards multiple
        genuinely qualified suppliers without inventing a composite supplier.
        """
        from ..reasoning.trajectory import BundleMetrics

        if mode == "service_quote_required":
            qualified = [
                c for c in candidates
                if (isinstance(c, ServiceCandidate) and c.has_evidence()
                    and c.service_match_evidence and not self._has_finalization_conflict(c))
            ]
        elif mode == "contact_enrichment_only":
            qualified = [
                c for c in candidates
                if (isinstance(c, ContactCandidate) and c.has_evidence()
                    and c.contacts and not self._has_finalization_conflict(c))
            ]
        else:
            qualified = [
                c for c in candidates
                if isinstance(c, ProductCandidate)
                and c.has_evidence()
                and c.pricing_status in _PRICED_STATUSES
                and not self._has_finalization_conflict(c)
            ]

        refs: list[EvidenceRef] = []
        seen: set[str] = set()
        for cand in qualified:
            for ref in cand.evidence_refs:
                if ref.ledger_id not in seen:
                    seen.add(ref.ledger_id)
                    refs.append(ref)
        hosts = {urlparse(r.url).netloc for r in refs if r.url}
        diversity = round(min(1.0, len(hosts) / 3.0), 4) if refs else 0.0
        count = len(qualified)
        metrics = BundleMetrics(
            evidence_diversity=diversity,
            qualified_supplier_coverage=round(min(1.0, count / 3.0), 4),
        )
        disputed, conflict = 0, 0.0

        if mode == "service_quote_required":
            svc = [c for c in qualified if isinstance(c, ServiceCandidate)]
            if svc:
                # Each input is candidate-local.  The quote channel appears in
                # exactly one metric (contactability), never evidence quality.
                metrics.service_match = round(sum(
                    min(1.0, max(0.0, c.service_match_score)) for c in svc
                ) / len(svc), 4)
                metrics.quote_channel = round(sum(
                    self._rankers["service"].components(c)["contactability"] / 25.0
                    for c in svc
                ) / len(svc), 4)
                metrics.geo = round(sum(
                    min(1.0, max(0.0, c.geo_score) / 20.0) for c in svc
                ) / len(svc), 4)
                metrics.checklist = round(sum(
                    min(1.0, max(0.0, c.checklist_completeness)) for c in svc
                ) / len(svc), 4)
                metrics.evidence_quality = round(sum(
                    min(1.0, max(0.0, c.evidence_completeness)) for c in svc
                ) / len(svc), 4)
                conflict = round(max((c.conflict_penalty for c in svc), default=0.0), 4)
                disputed = sum(1 for c in svc if c.conflict_penalty > 0)
        elif mode == "contact_enrichment_only":
            contacts = [c for c in qualified if isinstance(c, ContactCandidate)]
            if contacts:
                metrics.geo = round(sum(
                    min(1.0, max(0.0, c.geo_score) / 20.0) for c in contacts
                ) / len(contacts), 4)
                metrics.evidence_quality = round(sum(
                    min(1.0, max(0.0, c.evidence_completeness)) for c in contacts
                ) / len(contacts), 4)
        else:
            # Product candidates currently carry neither authorised-distributor
            # nor current-stock claims.  Geography and a discovered price are not
            # substitutes for those facts, so these evidence-dependent metrics
            # deliberately remain zero until dedicated extractors provide them.
            pass
        return metrics, refs, disputed, conflict, count

    # --- pipeline phases --------------------------------------------------
    async def _gather(
        self,
        ctx: ExecutionContext,
        route: RoutePlan,
        query: str,
        search: SearchService,
        fetch: FetchService,
        *,
        region: str,
        target_country: str | None,
        reserve_search_calls: int = 0,
        pages_out: list | None = None,
        queries_out: list | None = None,
    ) -> list:
        templates = build_query_templates(
            query, region=region, target_country=target_country, mode=route.mode.value
        )
        expanded = self.planner.expand_query(query, mode=route.mode.value)
        max_queries = ctx.tracker.remaining_search_calls()
        if reserve_search_calls:
            max_queries = max(0, max_queries - reserve_search_calls)
        queries = merge_gather_queries(templates, expanded, max_queries=max_queries)
        if queries_out is not None:
            queries_out.extend(queries)
        ctx.tracer.record(
            step="query_expand", tool="query_rewrite", status="success",
            input_count=1, output_count=len(queries),
            detail={"region": region, "kinds": sorted({sq.kind for sq in expanded}),
                    "queries": queries[:12]},
        )
        location = None if region == "global" else self.geo.location_code(target_country)
        if self.frontier_enabled:
            return await self._gather_frontier(
                ctx, route, queries, search, fetch,
                location=location, target_country=target_country,
                reserve_search_calls=reserve_search_calls, pages_out=pages_out,
            )
        return await self._gather_queries(
            ctx, route, queries, search, fetch,
            location=location, target_country=target_country,
            reserve_search_calls=reserve_search_calls, pages_out=pages_out,
        )

    async def _gather_frontier(
        self,
        ctx: ExecutionContext,
        route: RoutePlan,
        queries: list[str],
        search: SearchService,
        fetch: FetchService,
        *,
        location: str | None,
        target_country: str | None,
        reserve_search_calls: int = 0,
        pages_out: list | None = None,
    ) -> list:
        """Flagged frontier gather: one scored priority queue of queries and URLs,
        drained best-first within the same budget caps as the linear path.

        Each round: pop query leads -> search -> SERP results enter as scored
        url leads; pop the best url leads -> fetch/extract; 1-hop page links
        (same-domain contact pages, directory entries) re-enter the queue and
        compete on score. Stops on empty queue, budget caps, or MAX_ROUNDS.
        """
        from .frontier import MAX_ROUNDS, SCORE_FLOOR

        priors = self.policy.source_reliability()
        target_cc = self.geo.location_code(target_country)
        frontier = Frontier()
        for i, q in enumerate(queries):
            frontier.add(Lead(kind="query", value=q, provenance="template",
                              score=max(SCORE_FLOOR, 0.5 - 0.01 * i)))

        candidates: list = []
        rounds = 0
        link_leads_inserted = 0
        entity_leads_inserted = 0
        entity_urls_fetched = 0
        scorer_moved = 0
        while rounds < MAX_ROUNDS and not ctx.tracker.runtime_exceeded():
            rounds += 1
            # Entity follow-ups are still possible in round 1 (nothing fetched
            # yet) or while entity queries wait in the queue; only then is part
            # of the budget held back for them.
            entity_possible = rounds == 1 or frontier.pending("query", provenance="entity_query") > 0
            n_search = max(0, ctx.tracker.remaining_search_calls() - reserve_search_calls)
            entity_queries = frontier.pop("query", n_search, provenance="entity_query")
            search_hold = _ENTITY_SEARCH_RESERVE if entity_possible and not entity_queries else 0
            search_hold = min(search_hold, max(0, n_search - 1))
            other_queries = frontier.pop(
                "query", max(0, n_search - len(entity_queries) - search_hold))
            query_leads = entity_queries + other_queries
            if entity_queries:
                # Searched separately so their SERP leads carry entity
                # provenance and may draw on the reserved fetch slots.
                results = await self._collect_search_results(
                    ctx, [l.value for l in entity_queries], search,
                    location=location, reserve_search_calls=reserve_search_calls,
                )
                for lead in leads_from_search_results(
                    results, query=ctx.query, priors=priors, target_cc=target_cc,
                ):
                    lead.provenance = "entity_serp"
                    frontier.add(lead)
            if other_queries:
                results = await self._collect_search_results(
                    ctx, [l.value for l in other_queries], search,
                    location=location, reserve_search_calls=reserve_search_calls,
                )
                for lead in leads_from_search_results(
                    results, query=ctx.query, priors=priors, target_cc=target_cc,
                ):
                    frontier.add(lead)

            scorer_moved += self._qwen_rescore_frontier(ctx, frontier)

            can_extract = ctx.tracker.budget.max_candidates_to_extract - ctx.tracker.candidates_extracted
            can_fetch = ctx.tracker.budget.max_fetch_urls - ctx.tracker.fetch_urls
            budget_now = min(can_extract, can_fetch)
            url_leads = frontier.pop("url", budget_now, provenance="entity_serp")
            entity_urls_fetched += len(url_leads)
            fetch_hold = _ENTITY_FETCH_RESERVE if entity_possible and not url_leads else 0
            fetch_hold = min(fetch_hold, max(0, budget_now - len(url_leads) - 1))
            url_leads = url_leads + frontier.pop(
                "url", max(0, budget_now - len(url_leads) - fetch_hold))
            if not query_leads and not url_leads:
                break
            if not url_leads:
                continue
            ctx.tracer.record(
                step="frontier_drain", tool="frontier", status="success",
                input_count=len(url_leads), output_count=len(url_leads),
                detail={"round": rounds,
                        "leads": [{"url": l.value, "score": l.score, "provenance": l.provenance,
                                   "depth": l.depth} for l in url_leads[:8]]},
            )
            round_pages: list = []
            candidates.extend(await self._fetch_and_extract(
                ctx, route, [l.value for l in url_leads], fetch, target_country, round_pages,
            ))
            if pages_out is not None:
                pages_out.extend(round_pages)
            for page in round_pages:
                frontier.mark_seen(Lead(kind="url", value=page.final_url or page.url))
                for lead in link_leads_from_page(page, query=ctx.query, priors=priors, target_cc=target_cc):
                    link_leads_inserted += frontier.add(lead)
            entity_leads_inserted += self._insert_entity_query_leads(frontier, candidates, target_country)
            entity_leads_inserted += self._insert_vendor_mention_leads(ctx, frontier, target_country)

        stats = {
            "enabled": True, "rounds": rounds,
            "link_leads_inserted": link_leads_inserted,
            "entity_query_leads_inserted": entity_leads_inserted,
            "entity_url_leads_fetched": entity_urls_fetched,
            "pending_at_stop": frontier.pending(),
            **frontier.stats,
        }
        if scorer_moved:
            stats["qwen_scorer_moved"] = scorer_moved
        # A replan/broadening pass runs a fresh frontier; counters accumulate
        # across gathers (pending_at_stop stays the last gather's honest value).
        prev = ctx.metadata.get("frontier")
        if isinstance(prev, dict) and prev.get("enabled"):
            for key in ("rounds", "link_leads_inserted", "entity_query_leads_inserted",
                        "entity_url_leads_fetched", "added", "dropped_floor",
                        "dropped_depth", "dropped_duplicate", "popped"):
                stats[key] = stats.get(key, 0) + prev.get(key, 0)
            merged_moved = stats.get("qwen_scorer_moved", 0) + prev.get("qwen_scorer_moved", 0)
            if merged_moved:
                stats["qwen_scorer_moved"] = merged_moved
            stats["gathers"] = prev.get("gathers", 1) + 1
        ctx.metadata["frontier"] = stats
        deduped, frontier_merges = dedupe_candidates(candidates)
        self._record_consolidation(
            ctx.tracer, len(candidates), deduped, frontier_merges,
        )
        return deduped

    def _qwen_rescore_frontier(self, ctx: ExecutionContext, frontier: Frontier) -> int:
        """Stage-3 seam (off unless QWEN_FRONTIER_SCORER_ENABLED): Qwen proposes
        per-lead deltas, the frontier clamps and applies them. Reorder only."""
        if self.qwen_frontier_scorer is None:
            return 0
        leads = frontier.url_leads()
        if not leads:
            return 0
        try:
            deltas = self.qwen_frontier_scorer(ctx.query, [(l.value, l.score, l.provenance) for l in leads])
        except Exception as exc:
            ctx.tracer.record(step="frontier_rescore", tool="qwen_frontier_scorer",
                              status="error", error=str(exc))
            return 0
        moved = apply_scorer_deltas(leads, deltas)
        if moved:
            ctx.tracer.record(step="frontier_rescore", tool="qwen_frontier_scorer",
                              status="success", input_count=len(leads), output_count=moved)
        return moved

    def _collect_vendor_mentions(self, ctx: ExecutionContext, qwen: QwenPageExtraction, page_text: str) -> None:
        """Stash grounded vendor names Qwen found on a multi-vendor page
        (listicle entries, award winners) for follow-up query leads."""
        mentions: list[str] = ctx.metadata.setdefault("qwen_vendor_mentions", [])
        for m in qwen.vendor_mentions[:_VENDOR_MENTIONS_PER_PAGE]:
            name = (m.name or "").strip()
            if name and name not in mentions and value_grounded(name, page_text or ""):
                mentions.append(name)

    def _insert_vendor_mention_leads(self, ctx: ExecutionContext, frontier: Frontier, target_country: str | None) -> int:
        names = ctx.metadata.pop("qwen_vendor_mentions", [])
        return sum(frontier.add(entity_query_lead(name, target_country)) for name in names)

    def _insert_entity_query_leads(self, frontier: Frontier, candidates: list, target_country: str | None) -> int:
        """Vendors we extracted but could not ground a quote channel for earn a
        targeted follow-up query lead (depth 1)."""
        inserted = 0
        for cand in candidates:
            if not isinstance(cand, ServiceCandidate) or cand.quote_channel is not None:
                continue
            if not cand.vendor_name or cand.vendor_name == "Unknown Vendor":
                continue
            inserted += frontier.add(entity_query_lead(cand.vendor_name, target_country))
        return inserted

    async def _gather_queries(
        self,
        ctx: ExecutionContext,
        route: RoutePlan,
        queries: list[str],
        search: SearchService,
        fetch: FetchService,
        *,
        location: str | None,
        target_country: str | None,
        reserve_search_calls: int = 0,
        pages_out: list | None = None,
    ) -> list:
        """Search the given query strings, fetch, and build candidates.

        Candidates always match against the buyer's original query (``ctx.query``),
        even when ``queries`` are expanded/corrective variants.
        """
        results = await self._collect_search_results(
            ctx, queries, search, location=location, reserve_search_calls=reserve_search_calls,
        )
        urls = self._prioritize_fetch_urls(ctx, results, target_country)
        return await self._fetch_and_extract(ctx, route, urls, fetch, target_country, pages_out)

    async def gather_parallel(
        self,
        ctx: ExecutionContext,
        route: RoutePlan,
        queries: list[str],
        search: SearchService,
        fetch: FetchService,
        *,
        location: str | None,
        target_country: str | None,
        pages_out: list | None = None,
    ) -> list:
        """T-1.4: concurrent searches + fetches via the LLM-Compiler DAG.

        Used by the width-first GRAM-lite mode (T-3.3). Work is capped to the
        remaining search/fetch budget and rate-limited by token buckets.
        """
        results = await self._collect_search_results(ctx, queries, search, location=location)
        urls = self._prioritize_fetch_urls(ctx, results, target_country)
        return await self._fetch_and_extract(ctx, route, urls, fetch, target_country, pages_out)

    def _prioritize_fetch_urls(self, ctx: ExecutionContext, results: list, target_country: str | None) -> list[str]:
        """Order SERP results by fetch-worthiness before any budget is spent.

        The snippet/title the SERP already paid for prices each fetch:
        reliability prior + term overlap + geo TLD. Ties keep discovery order
        (stable sort), so equal-scored results behave as before.
        """
        leads = rank_leads(leads_from_search_results(
            results, query=ctx.query, priors=self.policy.source_reliability(),
            target_cc=self.geo.location_code(target_country),
        ))
        if leads:
            ctx.tracer.record(
                step="frontier_score", tool="frontier", status="success",
                input_count=len(leads), output_count=len(leads),
                detail={"top": [{"url": l.value, "score": l.score} for l in leads[:5]]},
            )
        return [l.value for l in leads]

    async def _collect_search_results(
        self,
        ctx: ExecutionContext,
        queries: list[str],
        search: SearchService,
        *,
        location: str | None,
        reserve_search_calls: int = 0,
    ) -> list:
        """Run one or many search queries; return URL-deduped SearchResults in
        discovery order (titles/snippets kept for pre-fetch scoring)."""
        max_searches = ctx.tracker.remaining_search_calls()
        if reserve_search_calls:
            max_searches = max(0, max_searches - reserve_search_calls)
        budgeted = list(queries)[:max_searches]
        if not budgeted:
            return []

        collected: list = []
        seen: set[str] = set()

        def _add(result_set) -> None:
            if result_set is None:
                return
            for r in result_set.results:
                if r.url not in seen:
                    seen.add(r.url)
                    collected.append(r)

        if len(budgeted) == 1:
            if ctx.tracker.can_search():
                try:
                    _add(await search.search(budgeted[0], location=location))
                except BudgetExceeded:
                    pass
            return collected

        def _search_node(q: str):
            async def run(_dep):
                if not ctx.tracker.can_search():
                    return None
                try:
                    return await search.search(q, location=location)
                except BudgetExceeded:
                    return None
            return run

        nodes = [ToolNode(id=f"search_{i}", kind="search", run=_search_node(q))
                 for i, q in enumerate(budgeted)]
        results, _trace = await self.compiler.execute(nodes, tracer=ctx.tracer)
        for rs in results.values():
            _add(rs)
        return collected

    async def _fetch_and_extract(
        self,
        ctx: ExecutionContext,
        route: RoutePlan,
        urls: list[str],
        fetch: FetchService,
        target_country: str | None,
        pages_out: list | None = None,
    ) -> list:
        urls = _dedupe(urls)[: ctx.tracker.budget.max_candidates_to_extract]
        ctx.working.add_urls(urls)
        if not urls or not ctx.tracker.can_fetch():
            return []

        pages = await self._fetch_pages_parallel(ctx, urls, fetch)
        if pages_out is not None:
            pages_out.extend(pages)

        candidates = []
        for page in pages:
            if not page.text:
                continue
            if not ctx.tracker.consume_extraction():
                break
            cand = self._build_candidate(ctx, route, ctx.query, page, target_country)
            if cand is not None:
                candidates.append(cand)
        return candidates

    async def _fetch_pages_parallel(
        self,
        ctx: ExecutionContext,
        urls: list[str],
        fetch: FetchService,
    ) -> list:
        """Fetch URLs concurrently (T-1.4); single-URL path stays a direct call."""
        if len(urls) <= 1:
            try:
                fetched = await fetch.fetch(urls)
            except BudgetExceeded:
                return []
            ctx.working.add_fetched([p.final_url or p.url for p in fetched.results])
            return list(fetched.results)

        def _fetch_node(url: str):
            async def run(_dep):
                try:
                    return await fetch.fetch([url])
                except BudgetExceeded:
                    return None
            return run

        nodes = [ToolNode(id=f"fetch_{i}", kind="fetch", run=_fetch_node(u))
                 for i, u in enumerate(urls)]
        results, _trace = await self.compiler.execute(nodes, tracer=ctx.tracer)
        pages = []
        for rs in results.values():
            if rs is not None:
                pages.extend(rs.results)
        ctx.working.add_fetched([p.final_url or p.url for p in pages])
        return pages

    def _recall_memory(
        self,
        query: str,
        ctx: ExecutionContext,
        audit: AuditLog,
        *,
        reference_ts: str,
    ) -> list[MemoryRecall]:
        if self.memory_mcp is None:
            return []
        try:
            if hasattr(self.memory_mcp, "memory"):
                self.memory_mcp.memory.maintain(reference_ts=reference_ts)
            recalls = self.memory_mcp.recall(
                query=query,
                top_k=5,
                context_budget_chars=1200,
                reference_ts=reference_ts,
            )
        except Exception as exc:
            ctx.tracer.record(step="memory_recall", tool="semantic_memory", status="error", error=str(exc))
            return []
        if recalls:
            audit.record("semantic_memory_recalled", count=len(recalls))
            ctx.tracer.record(
                step="memory_recall",
                tool="semantic_memory",
                status="success",
                input_count=1,
                output_count=len(recalls),
            )
        return recalls

    def _apply_memory_recalls(self, ctx: ExecutionContext, candidates: list, recalls: list[MemoryRecall]) -> list:
        # SCOPE (v1): only quote_channel facts on ServiceCandidates are attached
        # here, and this is the ONLY writer of semantic_memory ledger rows -- so
        # citation crediting (memory/citation_rank.py) can reach no other fact
        # field or mode yet. Widening recall coverage means widening this method.
        if not recalls:
            return candidates
        # Guardrail (defense-in-depth): disputed facts must not reach an RFQ draft.
        # The primary gate is upstream -- recall returns active-only facts -- so this
        # rfq_eligible re-check is a boundary guard; the allow_disputed policy flag
        # only takes effect if a recall backend ever surfaces a non-active fact.
        eligible = rfq_eligible(recalls, allow_disputed=self.policy.allow_disputed_facts_in_rfq)
        quote_recalls = [r for r in eligible if r.fact.field == "quote_channel"]
        if not quote_recalls:
            return candidates
        for cand in candidates:
            if not isinstance(cand, ServiceCandidate) or cand.quote_channel is not None:
                continue
            for recall in quote_recalls:
                if not _same_vendor(cand.vendor_name, recall.fact.entity_name):
                    continue
                ref = ctx.ledger.record(
                    source_tool="semantic_memory",
                    url=cand.website or "semantic-memory",
                    snippet=recall.fact.value,
                    confidence=recall.decayed_confidence,
                    metadata={
                        "fact_id": recall.fact.fact_id,
                        "field": recall.fact.field,
                        "source_evidence_refs": [r.model_dump(mode="json") for r in recall.fact.evidence_refs],
                        "recall_score": recall.score,
                    },
                )
                cand.quote_channel = QuoteChannel(
                    type=_quote_type_from_memory(recall.fact.value),
                    value=recall.fact.value,
                    evidence_ref=ref,
                )
                cand.evidence_refs = _merge_refs(cand.evidence_refs, [ref])
                cand.evidence_completeness = max(cand.evidence_completeness, 0.75)
                break
        return candidates

    @staticmethod
    def _apply_consolidation_safety(candidates: list) -> list:
        """Downgrade unresolved merged claims before ranking or finalization.

        Consolidation can legitimately combine complementary pages, but competing
        field values are unresolved observations, not a stronger selected fact.
        Keep them visible on the candidate for review while preventing a conflict
        from meeting the normal completeness gate.  Service rankers also receive
        the bounded penalty before their score is calculated.
        """
        for candidate in candidates:
            if not Controller._has_finalization_conflict(candidate):
                continue
            candidate.evidence_completeness = min(candidate.evidence_completeness, 0.5)
            if isinstance(candidate, ServiceCandidate):
                candidate.conflict_penalty = min(candidate.conflict_penalty, -20.0)
        return candidates

    @staticmethod
    def _record_consolidation(
        tracer: Tracer,
        input_count: int,
        candidates: list,
        merge_count: int,
    ) -> None:
        """Trace the real supplier identity pass without exposing supplier data."""
        tracer.record(
            step="supplier_consolidation",
            tool="supplier_identity",
            status="success",
            input_count=input_count,
            output_count=len(candidates),
            detail={"merged_candidates": merge_count},
        )

    @staticmethod
    def _has_finalization_conflict(candidate) -> bool:
        """A true consolidated conflict is withheld from finalized output.

        Identity consolidation keeps compatible alternatives (for example, two
        RFQ-form paths) in ``field_claims`` without placing them here. Every
        entry in ``conflicting_fields`` is therefore an unresolved assertion
        that must not become a validated supplier, RFQ, or memory fact.
        """
        return bool(getattr(candidate, "conflicting_fields", None))

    def _build_candidate(self, ctx: ExecutionContext, route: RoutePlan, query: str, page, target_country: str | None):
        meta = self._extractors["vendor_metadata"].extract(
            page_url=page.url, final_url=page.final_url, title=page.title, text=page.text
        )
        ref = page.evidence_ref
        refs = [ref] if ref else []
        geo_score = self.geo.score(meta.country, target_country, page.text)
        page_url = page.final_url or page.url
        if _marketplace_listing_page(page_url):
            ctx.tracer.record(
                step="page_role_gate", tool="url_heuristic", status="success",
                input_count=1, output_count=0,
                detail={"url": page_url, "page_role": "directory",
                        "reason": "marketplace_listing"},
            )
            return None
        qwen = self._extract_qwen_json(ctx, query, page_url, page.text)
        if qwen is not None:
            self._collect_vendor_mentions(ctx, qwen, page.text)
            # Qwen proposes a vendor name; it only replaces the title heuristic
            # when the proposed name is grounded in the page text.
            vendor_name = (qwen.vendor.name or "").strip()
            if vendor_name and value_grounded(vendor_name, page.text or ""):
                meta.vendor_name = vendor_name
            # Suppression is tighten-only: Qwen may drop a buyer-side or
            # multi-vendor page from candidates, never promote one.
            if qwen.page_role in _NON_VENDOR_PAGE_ROLES:
                ctx.tracer.record(
                    step="page_role_gate", tool="qwen_json_extractor", status="success",
                    input_count=1, output_count=0,
                    detail={"url": page_url, "page_role": qwen.page_role},
                )
                return None

        if route.ranker == "product":
            return self._product_candidate(ctx, query, page, meta, refs, geo_score, page_url, qwen)
        if route.ranker == "service":
            return self._service_candidate(ctx, query, page, meta, refs, geo_score, page_url, target_country, qwen)
        return self._contact_candidate(ctx, page, meta, refs, geo_score, qwen)

    def _extract_qwen_json(self, ctx: ExecutionContext, query: str, page_url: str, text: str) -> QwenPageExtraction | None:
        if self.qwen_json_extractor is None:
            return None
        try:
            result = self.qwen_json_extractor.extract(text=text, page_url=page_url, query=query)
        except Exception as exc:
            # Any extractor/provider failure degrades to the deterministic path.
            ctx.tracer.record(step="qwen_json_extract", tool="qwen_json_extractor", status="error", error=str(exc))
            return None
        ctx.tracer.record(step="qwen_json_extract", tool="qwen_json_extractor", status="success", input_count=1, output_count=1)
        ctx.metadata["qwen_json_extractions"] = ctx.metadata.get("qwen_json_extractions", 0) + 1
        return result

    def _product_candidate(self, ctx, query, page, meta, refs, geo_score, page_url, qwen: QwenPageExtraction | None):
        pricing = self._extractors["pricing"].extract(page.text, page_url=page_url)
        if qwen and qwen.pricing.status != PricingStatus.NOT_FOUND:
            pricing = PricingResult(
                status=qwen.pricing.status,
                price=qwen.pricing.price,
                currency=qwen.pricing.currency,
                unit=qwen.pricing.unit,
                matched_text=qwen.pricing.matched_text,
            )
            # Tighten-only subject check: when Qwen names what the price is
            # for and that grounded subject shares no term with the buyer
            # query, the price belongs to something else on the page (an
            # accessory, an add-on) and is dropped, not attributed.
            subject = (qwen.pricing.subject or "").strip()
            if (
                subject
                and value_grounded(subject, page.text or "")
                and term_overlap(query, subject) == 0.0
            ):
                ctx.tracer.record(
                    step="pricing_subject_gate", tool="qwen_json_extractor",
                    status="success", input_count=1, output_count=0,
                    detail={"url": page_url, "subject": subject},
                )
                pricing = PricingResult(status=PricingStatus.NOT_FOUND)
        pricing_ref = self._record_extraction_ref(
            ctx, page, "pricing", pricing.matched_text
        ) if pricing.matched_text else None
        candidate_refs = _merge_refs(refs, [pricing_ref] if pricing_ref else [])
        moq_match = _MOQ_RE.search(page.text)
        cand = ProductCandidate(
            vendor_name=meta.vendor_name,
            website=meta.website,
            country=meta.country,
            geo_score=geo_score,
            evidence_refs=candidate_refs,
            product_name=query,
            price=pricing.price,
            currency=pricing.currency,
            unit=pricing.unit,
            moq=moq_match.group(1) if moq_match else None,
            pricing_status=pricing.status,
            product_url=page_url,
        )
        backed = [
            bool(refs and meta.vendor_name != "Unknown Vendor"),
            pricing.status in _PRICED_STATUSES and pricing_ref is not None,
            bool(page_url),
        ]
        cand.evidence_completeness = round(sum(backed) / len(backed), 3)
        return cand

    def _service_candidate(self, ctx, query, page, meta, refs, geo_score, page_url, target_country, qwen: QwenPageExtraction | None):
        sm = self._extractors["service_match"].extract(query, page.text)
        service_ref = self._record_extraction_ref(
            ctx, page, "service_match", "", fallback_terms=sm.matched_terms
        ) if sm.matched else None
        qc_matches = self._extractors["quote_channel"].extract(page.text, page.links, page_url)
        if qwen:
            # Untrusted model output: an empty-valued channel must not enter the
            # pool -- the deterministic extractor enforces the same guard, and a
            # critical claim with no concrete value can never verify.
            qc_matches.extend(
                QuoteChannelMatch(type=q.type, value=q.value, matched_text=q.matched_text or q.value)
                for q in qwen.quote_channels if (q.value or "").strip()
            )
        best = self._extractors["quote_channel"].best(qc_matches)
        if self.verify_claims:
            # Same groundedness the verification spine applies later: quote
            # channels are vendor-scoped relation claims, so prefer a match
            # co-located with the vendor in one sentence, then a value-grounded
            # one -- a value-only preference can pick a channel the spine then
            # rejects while discarding one that would have verified.
            subject = meta.vendor_name or ""
            relation = [
                m for m in qc_matches
                if relation_grounded(subject, m.value or "", page.text or "")
            ]
            grounded = relation or [
                m for m in qc_matches if value_grounded(m.value or "", page.text or "")
            ]
            best = self._extractors["quote_channel"].best(grounded) or best
        quote_channel = None
        quote_ref = None
        if best and refs:
            quote_ref = self._record_extraction_ref(
                ctx, page, "quote_channel", best.value or best.matched_text
            )
            quote_channel = QuoteChannel(type=best.type, value=best.value, evidence_ref=quote_ref or refs[0])
        pricing = self._extractors["pricing"].extract(page.text, page_url=page_url)
        candidate_refs = _merge_refs(refs, [r for r in (service_ref, quote_ref) if r is not None])
        cand = ServiceCandidate(
            vendor_name=meta.vendor_name,
            website=meta.website,
            country=meta.country,
            geo_score=geo_score,
            evidence_refs=candidate_refs,
            service_match_score=sm.score,
            service_match_evidence=sm.matched,
            pricing_status=pricing.status if pricing.status != PricingStatus.NOT_FOUND else PricingStatus.QUOTE_REQUIRED,
            quote_channel=quote_channel,
        )
        backed = [
            bool(refs and meta.vendor_name != "Unknown Vendor"),
            sm.matched and service_ref is not None,
            quote_channel is not None,
        ]
        cand.evidence_completeness = round(sum(backed) / len(backed), 3)
        return cand

    def _contact_candidate(self, ctx, page, meta, refs, geo_score, qwen: QwenPageExtraction | None):
        matches = self._extractors["contact"].extract(page.text, page.links)
        if qwen:
            from ..extraction.contact import ContactMatch

            matches.extend(
                ContactMatch(
                    type=c.type,
                    value=c.value,
                    confidence=c.confidence,
                    privacy_class=c.privacy_class,
                )
                for c in qwen.contacts
            )
        ref = refs[0] if refs else None
        site_domain = _registrable(meta.website)
        contacts = []
        domain_match = False
        contact_refs = []
        for m in matches:
            if not ref:
                continue
            contact_ref = self._record_extraction_ref(ctx, page, "contact", m.value) or ref
            contact_refs.append(contact_ref)
            if m.type == "email" and "@" in m.value:
                if _registrable("https://" + m.value.split("@", 1)[1]) == site_domain and site_domain:
                    domain_match = True
            contacts.append(
                Contact(
                    type=m.type, value=m.value, confidence=m.confidence,
                    privacy_class=m.privacy_class, evidence_ref=contact_ref,
                )
            )
        cand = ContactCandidate(
            vendor_name=meta.vendor_name,
            website=meta.website,
            country=meta.country,
            geo_score=geo_score,
            evidence_refs=_merge_refs(refs, contact_refs),
            contacts=contacts,
            validation_signals={
                "domain_match": domain_match,
                "marketplace_excluded": True,
                "cross_source_count": len(refs),
            },
        )
        backed = [bool(refs and meta.vendor_name != "Unknown Vendor"), bool(contacts)]
        cand.evidence_completeness = round(sum(backed) / len(backed), 3)
        return cand

    # --- verification (T-2.2) ---------------------------------------------
    def _verify_with_reserve(self, ledger: EvidenceLedger, qualified, tracer, budget):
        """Verify visible candidates, then refill from already-fetched evidence.

        The output cap is a presentation bound, not a reason to discard lower
        ranked candidates before their existing evidence has been checked.  This
        stays entirely inside the controller's extraction/search/fetch budget:
        reserve verification creates no provider calls and stops as soon as the
        minimum qualified output is restored.
        """
        initial = list(qualified[: budget.max_validated_candidates])
        kept, metrics = self._verify_candidates(ledger, initial, tracer)
        reserve_checked = 0
        for candidate in qualified[budget.max_validated_candidates:]:
            if len(kept) >= budget.min_validated_candidates:
                break
            refill, refill_metrics = self._verify_candidates(ledger, [candidate], tracer)
            kept.extend(refill)
            metrics = self._merge_verification_metrics(metrics, refill_metrics)
            reserve_checked += 1
        metrics["reserve_candidates_verified"] = reserve_checked
        return kept[: budget.max_validated_candidates], metrics

    @staticmethod
    def _merge_verification_metrics(first: dict, second: dict) -> dict:
        """Combine sequential verification batches without losing audit counts."""
        merged = dict(first)
        for key in ("claims_verified", "claims_unsupported", "candidates_blocked_unverified"):
            merged[key] = first.get(key, 0) + second.get(key, 0)
        merged["replan_recommended"] = bool(
            first.get("replan_recommended") or second.get("replan_recommended")
        )
        merged["verification_assessments"] = {
            **(first.get("verification_assessments") or {}),
            **(second.get("verification_assessments") or {}),
        }
        first_conformal = first.get("conformal")
        second_conformal = second.get("conformal")
        if first_conformal or second_conformal:
            conformal = dict(first_conformal or second_conformal or {})
            conformal["candidates_abstained"] = (
                (first_conformal or {}).get("candidates_abstained", 0)
                + (second_conformal or {}).get("candidates_abstained", 0)
            )
            merged["conformal"] = conformal
        return merged

    def _verify_candidates(self, ledger: EvidenceLedger, validated, tracer):
        """Verify each candidate's claims; drop those with unsupported critical claims."""
        spine = VerificationSpine(ledger, minicheck=self.minicheck or MiniCheck())
        kept = []
        verified_claims = unsupported_claims = blocked = abstained = 0
        replan_recommended = False
        # GSAR decision + GRADE per kept candidate: computing these and then
        # dropping them would make the trust signals invisible end to end.
        assessments: dict[str, dict] = {}
        for cand in validated:
            cv = spine.verify_candidate(cand)
            verified_claims += sum(1 for c in cv.claims if c.verified)
            unsupported_claims += sum(1 for c in cv.claims if not c.verified)
            # The worst GSAR decision across all candidates (kept or blocked)
            # drives the bounded replan round in run().
            replan_recommended = replan_recommended or cv.decision == "replan"
            if not cv.verified:
                blocked += 1
                if tracer is not None:
                    tracer.record(step="verify_claims", tool="minicheck_verifier", status="blocked",
                                  input_count=len(cv.claims), output_count=0,
                                  detail=cv.model_dump(mode="json"))
                continue
            # Statistical emission gate: a CALIBRATED abstention blocks the
            # candidate (its critical-claim verifier score falls below the LTT
            # selective-risk threshold). Uncalibrated decisions never gate --
            # no guarantee exists -- and the rationale surfaces in metrics below.
            if self.conformal is not None:
                decision = self.conformal.decide(cv.verifier_score)
                if decision.calibrated and decision.abstain:
                    abstained += 1
                    if tracer is not None:
                        tracer.record(step="conformal_gate", tool="conformal_abstainer",
                                      status="blocked", input_count=len(cv.claims), output_count=0,
                                      detail={"vendor": cv.vendor_name,
                                              **decision.model_dump(mode="json")})
                    continue
            assessments[self._assessment_key(cand)] = {
                "decision": cv.decision, "grade": cv.grade,
                "verifier_score": cv.verifier_score,
                "claims_verified": sum(1 for c in cv.claims if c.verified),
                "claims_unsupported": sum(1 for c in cv.claims if not c.verified),
            }
            if tracer is not None:
                tracer.record(step="verify_claims", tool="minicheck_verifier", status="success",
                              input_count=len(cv.claims), output_count=sum(c.verified for c in cv.claims),
                              detail={"vendor": cv.vendor_name, "verifier_score": cv.verifier_score,
                                      "decision": cv.decision, "grade": cv.grade})
            kept.append(cand)
        metrics = {"claims_verified": verified_claims, "claims_unsupported": unsupported_claims,
                   "candidates_blocked_unverified": blocked,
                   "verification_assessments": assessments,
                   "replan_recommended": replan_recommended}
        if self.conformal is not None:
            calibrated = self.conformal.threshold is not None
            delta = getattr(self.conformal, "delta", None)
            emitted = getattr(self.conformal, "calibration_emitted", None)
            wrong = getattr(self.conformal, "calibration_wrong", None)
            metrics["conformal"] = {
                "calibrated": calibrated,
                "threshold": self.conformal.threshold,
                "alpha": self.conformal.alpha,
                "delta": delta,
                # Explicit None when uncalibrated: "abstained 0/N" must not be
                # read as a guarantee statement when no guarantee exists.
                # risk_bound is the LTT selective-risk target: with confidence
                # 1-delta, P(wrong | emitted) <= alpha.
                "risk_bound": self.conformal.alpha if calibrated else None,
                "confidence": (
                    round(1.0 - delta, 4) if calibrated and delta is not None else None
                ),
                "candidates_abstained": abstained,
                "rationale": "; ".join(self.conformal.reasons)
                or (
                    f"calibrated on {self.conformal.calibration_size} examples"
                    + (
                        f" ({emitted} emitted at threshold, {wrong} wrong)"
                        if emitted is not None and wrong is not None else ""
                    )
                ),
            }
        return kept, metrics

    @staticmethod
    def _assessment_key(cand) -> str:
        """Trust data keyed by stable supplier identity when available."""
        supplier_id = getattr(cand, "supplier_id", "") or ""
        if supplier_id:
            return supplier_id
        name = getattr(cand, "vendor_name", "") or ""
        domain = _registrable(getattr(cand, "website", "") or "")
        return f"{name}|{domain}" if domain else name

    def _memory_credit_verified_candidates(self, ledger: EvidenceLedger, candidates, tracer):
        """Narrow verification pass used only to credit recalled memory facts.

        Global claim verification may be disabled for the user-facing pipeline,
        but citation counts still must not self-reinforce. Only candidates that
        cite a semantic_memory row and pass the same spine against the current
        corpus are eligible for memory citation credit.
        """
        spine = VerificationSpine(ledger, minicheck=self.minicheck or MiniCheck())
        kept = []
        for cand in candidates:
            if not self._has_semantic_memory_refs(ledger, [cand]):
                continue
            cv = spine.verify_candidate(cand)
            if cv.verified:
                kept.append(cand)
                if tracer is not None:
                    tracer.record(
                        step="memory_credit_verify",
                        tool="minicheck_verifier",
                        status="success",
                        input_count=len(cv.claims),
                        output_count=len(cv.claims),
                        detail={"vendor": cv.vendor_name, "verifier_score": cv.verifier_score},
                    )
            elif tracer is not None:
                tracer.record(
                    step="memory_credit_verify",
                    tool="minicheck_verifier",
                    status="blocked",
                    input_count=len(cv.claims),
                    output_count=0,
                    detail=cv.model_dump(mode="json"),
                )
        return kept

    @staticmethod
    def _has_semantic_memory_refs(ledger: EvidenceLedger, candidates) -> bool:
        for cand in candidates:
            for ref in getattr(cand, "evidence_refs", None) or []:
                item = ledger.get(getattr(ref, "ledger_id", ""))
                if item is not None and item.source_tool == "semantic_memory":
                    return True
        return False

    # --- validation / stop ------------------------------------------------
    def _is_validated(self, candidate, mode: ProcurementMode, budget) -> bool:
        if not candidate.has_evidence():
            return False
        # A consolidated conflict preserves both evidenced alternatives for
        # review. It cannot become a finalized supplier, RFQ, or memory fact.
        if self._has_finalization_conflict(candidate):
            return False
        if candidate.evidence_completeness < budget.evidence_completeness_threshold:
            return False
        if mode in (ProcurementMode.PRODUCT_EXACT_PRICE, ProcurementMode.ELECTRONICS_SUBSTITUTION):
            return candidate.pricing_status in _PRICED_STATUSES
        if mode == ProcurementMode.SERVICE_QUOTE_REQUIRED:
            return candidate.quote_channel is not None and candidate.service_match_evidence
        if mode in (ProcurementMode.CONTACT_ENRICHMENT_ONLY, ProcurementMode.REVALIDATION):
            return bool(getattr(candidate, "contacts", []))
        return False

    def _stop_reason(self, mode, validated, candidates, tracker: BudgetTracker, budget) -> StopReason:
        if len(validated) >= budget.min_validated_candidates:
            return StopReason.MIN_VALIDATED_CANDIDATES_MET
        if tracker.stop_reason is not None:
            return tracker.stop_reason
        if mode == ProcurementMode.SERVICE_QUOTE_REQUIRED and not any(
            isinstance(c, ServiceCandidate) and c.quote_channel is not None for c in candidates
        ):
            return StopReason.NO_QUOTE_CHANNEL_FOUND
        if tracker.runtime_exceeded():
            return StopReason.MAX_RUNTIME_REACHED
        return StopReason.INSUFFICIENT_EVIDENCE

    def _build_rfqs(
        self,
        query,
        validated,
        target_country,
        metrics: Metrics,
        audit: AuditLog,
        run_id: str,
        review_store: ReviewStore | None,
        *,
        ledger: EvidenceLedger | None = None,
        assessments: dict[str, dict[str, str]] | None = None,
    ) -> list[dict]:
        generator = RFQGenerator(
            tone=self.policy.rfq_tone,
            minimum_completeness=self.policy.minimum_checklist_completeness,
            drafter=self.qwen_rfq_drafter,
        )
        assessments = assessments or {}
        grade_floor = self.policy.rfq_grade_floor
        drafts: list[dict] = []
        for cand in validated:
            if not isinstance(cand, ServiceCandidate):
                continue
            evidence_grade = (assessments.get(self._assessment_key(cand)) or {}).get("grade")
            draft = generator.generate(
                query=query, candidate=cand, target_country=target_country,
                # Trust surface on the draft itself: the spine's GRADE for this
                # candidate (None when verification is off) and the DS interval
                # fused over the quote channel's source reliabilities.
                evidence_grade=evidence_grade,
                belief_interval=quote_channel_interval(cand, ledger),
                # Fact-check corpus for a Qwen-drafted body: the candidate's
                # own ledger evidence, so unsourced numbers are flagged.
                evidence_corpus=self._evidence_corpus(cand, ledger),
            )
            metrics.rfq_drafts_total += 1
            if draft.status == "incomplete":
                metrics.rfq_incomplete_total += 1
            # GRADE-style two-axis policy: the grade is advisory metadata; the
            # policy floor is the action. Default floor "very_low" holds
            # nothing; a raised floor withholds low-certainty drafts for review.
            below_floor = bool(evidence_grade) and not grade_at_least(evidence_grade, grade_floor)
            audit.record("rfq_draft_generated", vendor=cand.vendor_name,
                         status="held_below_grade_floor" if below_floor else draft.status)
            draft_dict = draft.model_dump(mode="json")
            if review_store and (self.require_review or below_floor):
                # Blocking checkpoint: withhold the polished RFQ until a human
                # approves. The full draft is carried in the review event detail
                # and released by `review approve <event_id>`.
                event = review_store.create(
                    run_id=run_id,
                    reason=(
                        f"rfq finalization: evidence grade {evidence_grade} below "
                        f"policy floor {grade_floor}" if below_floor else "rfq finalization"
                    ),
                    proposed_action=f"review RFQ draft for {cand.vendor_name}",
                    detail={"vendor": cand.vendor_name, "status": draft.status, "rfq_draft": draft_dict},
                )
                metrics.held_for_review += 1
                drafts.append(
                    {
                        "schema_version": draft.schema_version,
                        "status": "pending_review",
                        "vendor": draft_dict.get("vendor"),
                        "quote_channel": draft_dict.get("quote_channel"),
                        "review_event_id": event.event_id,
                    }
                )
            elif below_floor:
                # No review store to hold the draft in: withhold the polished
                # body and say why, rather than releasing it or dropping it
                # silently.
                metrics.held_for_review += 1
                drafts.append(
                    {
                        "schema_version": draft.schema_version,
                        "status": "held_below_grade_floor",
                        "vendor": draft_dict.get("vendor"),
                        "quote_channel": draft_dict.get("quote_channel"),
                        "evidence_grade": evidence_grade,
                        "grade_floor": grade_floor,
                    }
                )
            else:
                # Advisory (non-blocking) review event when HITL is enabled.
                if review_store:
                    review_store.create(
                        run_id=run_id,
                        reason="rfq finalization",
                        proposed_action=f"review RFQ draft for {cand.vendor_name}",
                        detail={"vendor": cand.vendor_name, "status": draft.status},
                    )
                drafts.append(draft_dict)
        return drafts

    # --- helpers ----------------------------------------------------------
    def _evidence_corpus(self, candidate, ledger: EvidenceLedger | None) -> str:
        """Concatenated snippet/text of the candidate's cited ledger rows."""
        if ledger is None:
            return ""
        parts: list[str] = []
        for ref in getattr(candidate, "evidence_refs", None) or []:
            item = ledger.get(getattr(ref, "ledger_id", ""))
            if item is None:
                continue
            if item.snippet:
                parts.append(item.snippet)
            if item.text:
                parts.append(item.text)
        return " ".join(parts)

    def _build_trust_verdicts(
        self,
        validated,
        verification_metrics: dict,
        ledger: EvidenceLedger,
        disputed_signals,
    ) -> list[dict]:
        """One composed trust verdict per validated candidate.

        Pulls the fragments that previously lived only in metrics, RFQ
        assumptions, and S3 risk signals into a single per-vendor statement.
        """
        assessments = verification_metrics.get("verification_assessments") or {}
        conformal_meta = verification_metrics.get("conformal")
        from ..verification.readiness import candidate_readiness
        verdicts: list[dict] = []
        for cand in validated:
            name = getattr(cand, "vendor_name", "") or ""
            assessment = assessments.get(self._assessment_key(cand)) or {}
            interval = (
                quote_channel_interval(cand, ledger)
                if isinstance(cand, ServiceCandidate) else None
            )
            disputed = sorted({
                s.entity for s in disputed_signals or []
                if _same_vendor(name, s.entity)
            })
            parts: list[str] = []
            if not self.verify_claims:
                parts.append("claim verification disabled for this run")
            elif assessment:
                parts.append(
                    f"{assessment.get('claims_verified', 0)} verified claim(s), "
                    f"grade {assessment.get('grade') or 'n/a'}, "
                    f"decision {assessment.get('decision') or 'n/a'}"
                )
            if interval is not None:
                parts.append(
                    f"quote-channel belief [{interval.belief}, {interval.plausibility}]"
                )
            if conformal_meta is not None:
                if conformal_meta.get("calibrated"):
                    confidence = conformal_meta.get("confidence")
                    parts.append(
                        f"selective risk: P(wrong|emitted) <= {conformal_meta['risk_bound']}"
                        + (f" at confidence {confidence}" if confidence is not None else "")
                    )
                else:
                    parts.append("statistical emission guarantee unavailable (uncalibrated)")
            parts.append(
                f"{len(disputed)} disputed fact(s) flagged" if disputed else "no disputed facts"
            )
            verdicts.append({
                "supplier_id": getattr(cand, "supplier_id", "") or None,
                "vendor_name": name,
                "verification_enabled": self.verify_claims,
                "readiness": candidate_readiness(
                    cand, assessment, verification_enabled=self.verify_claims,
                    offline=self.offline, grade_floor=self.policy.rfq_grade_floor,
                    disputed=bool(disputed),
                ),
                "claims_verified": assessment.get("claims_verified"),
                "claims_unsupported": assessment.get("claims_unsupported"),
                "verifier_score": assessment.get("verifier_score"),
                "grade": assessment.get("grade"),
                "decision": assessment.get("decision"),
                "belief_interval": (
                    [interval.belief, interval.plausibility] if interval is not None else None
                ),
                "belief_uncertainty": interval.uncertainty if interval is not None else None,
                "conformal": conformal_meta,
                "disputed_facts": disputed,
                "summary": "; ".join(parts) + ".",
            })
        return verdicts

    def _qwen_paths_block(self, classification, json_extractions: int) -> dict:
        """Per-run audit of every Qwen seam: present, live or mocked, invoked.

        Honesty surface for demos: an offline judged run reports mock=true on
        each seam instead of implying live model calls happened.
        """
        def seam(obj, *, model=None, **extra) -> dict:
            info: dict = {"enabled": obj is not None}
            if obj is not None:
                name = type(obj).__name__
                info["implementation"] = name
                info["mock"] = "mock" in name.lower()
                if model:
                    info["model"] = model
            info.update(extra)
            return info

        nli = getattr(self.minicheck, "model", None) if self.minicheck is not None else None
        return {
            "offline": self.offline,
            "mode_router": seam(
                self.qwen_router, model=getattr(self.qwen_router, "model", None),
                invoked="qwen_tool_call" in (getattr(classification, "signals", None) or {}),
            ),
            "json_extractor": seam(
                self.qwen_json_extractor,
                model=getattr(self.qwen_json_extractor, "model", None),
                invocations=json_extractions,
            ),
            "nli_scorer": seam(nli, model=getattr(nli, "model", None)),
            "query_rewriter": seam(
                self.qwen_query_rewriter,
                model=getattr(self.qwen_query_rewriter, "model", None),
            ),
            "rfq_drafter": seam(
                self.qwen_rfq_drafter,
                model=getattr(self.qwen_rfq_drafter, "model", None),
            ),
            "page_judge": seam(self.page_judge),
        }

    def _pricing_summary(self, candidates) -> dict[str, int]:
        summary: dict[str, int] = {}
        for c in candidates:
            status = getattr(c, "pricing_status", None)
            if status is not None:
                summary[status.value] = summary.get(status.value, 0) + 1
        return summary

    def _public_candidate_dump(self, candidate) -> dict:
        data = candidate.model_dump(mode="json")
        if not isinstance(candidate, ContactCandidate):
            return data
        redacted = 0
        for contact in data.get("contacts", []):
            privacy_class = contact.get("privacy_class", "")
            if self.policy.review_gate_enabled(privacy_class):
                contact["value"] = "[redacted:high_sensitivity_contact]"
                contact["redaction_reason"] = "policy review gate required"
                redacted += 1
        if redacted:
            data.setdefault("validation_signals", {})["redacted_contacts"] = redacted
        return data

    def _public_withheld_candidate_dump(self, candidate) -> dict:
        """Serialize an audit-only conflicted candidate without promoting it."""
        data = self._public_candidate_dump(candidate)
        data["withheld_reason"] = "unresolved conflicting field claim"
        data["withheld_fields"] = list(getattr(candidate, "conflicting_fields", []) or [])
        return data

    def _record_extraction_ref(
        self,
        ctx: ExecutionContext,
        page,
        extraction: str,
        matched_text: str,
        *,
        fallback_terms: list[str] | None = None,
    ):
        if page.evidence_ref is None:
            return None
        snippet, start_char, end_char, span = _span_for_match(page.text, matched_text, fallback_terms)
        if not snippet:
            return None
        source_tool = getattr(page, "source_tool", "tinyfish_fetch")
        if source_tool not in _EVIDENCE_SOURCE_TOOLS:
            source_tool = "tinyfish_fetch"
        claim_id = f"claim_{sha256_hex(f'{page.evidence_ref.ledger_id}:{extraction}:{start_char}:{end_char}:{span}')[:12]}"
        metadata = {
            "extraction": extraction,
            "field": extraction,
            "matched_text": matched_text,
            "claim_id": claim_id,
            "parent_ledger_id": page.evidence_ref.ledger_id,
        }
        if start_char >= 0 and end_char >= start_char:
            metadata.update(
                {
                    "start_char": start_char,
                    "end_char": end_char,
                    "span_hash": sha256_hex(span),
                }
            )
        return ctx.ledger.record(
            source_tool=source_tool,
            url=page.url,
            final_url=page.final_url,
            title=page.title,
            snippet=snippet,
            language=page.language,
            confidence=0.75,
            metadata=metadata,
        )

    def _detect_target_country(self, query: str) -> str | None:
        lower = query.lower()
        for country in SEA_COUNTRIES:
            if country.lower() in lower:
                return country
        return None

    def _persist_run(
        self,
        ctx: ExecutionContext,
        audit: AuditLog,
        result: RunResult,
        validated,
        review_store: ReviewStore | None = None,
    ) -> None:
        outcome = "success" if result.stop_reason == StopReason.MIN_VALIDATED_CANDIDATES_MET.value else (
            "incomplete" if validated else "failed"
        )
        episodic = EpisodicMemory(self.state_dir)
        episodic.append(
            EpisodicRecord(
                query=result.query,
                mode=result.mode,
                summary=(
                    f"Considered {result.metrics.get('candidates_considered', 0)} candidates; "
                    f"validated {len(validated)}; {len(result.rfq_drafts)} RFQ draft(s)."
                ),
                evidence_refs=result.evidence_refs,
                outcome=outcome,
            )
        )
        if self.persist:
            self._persist_semantic(validated, result.run_id, review_store)
            ctx.ledger.persist()
            # Judge-verifiable citations: every final evidence ref ships its
            # inclusion proof against the commitment that persist() just
            # published (+ its signature when STH signing is configured).
            # Refs into OTHER runs' ledgers (recalled memory provenance) are
            # provable via `evidence prove <that run>`, not this run's log.
            seen: set[str] = set()
            ids = []
            for ref in result.evidence_refs:
                if ref.ledger_id not in seen and ctx.ledger.get(ref.ledger_id) is not None:
                    seen.add(ref.ledger_id)
                    ids.append(ref.ledger_id)
            result.citation_proofs = ctx.ledger.citation_proof_bundles(ids)
            self._persist_supplier_graph(ctx.ledger)
            ctx.tracer.persist() if ctx.tracer else None
            audit.persist()

    def _disputed_belief_signals(self, ledger: EvidenceLedger, ranked: list, audit: AuditLog):
        """S3 signals for disputed memory facts about this run's vendors.

        DS fusion (evidence/belief.py) turns each dispute into a [Bel, Pl]
        interval; wide gaps (or Yager-rule fusions) become RiskSignals so the
        uncertainty is surfaced in the run output instead of sitting silently
        in semantic memory. Scope: facts whose entity matches a ranked vendor
        -- unrelated disputes are not this run's risks. Every emitted signal
        is audited (never silent).
        """
        memory = self._semantic_memory()
        if memory is None or not ranked:
            return []
        disputed = [
            f for f in memory.all()
            if f.status == "disputed" and any(
                _same_vendor(getattr(c, "vendor_name", ""), f.entity_name) for c in ranked
            )
        ]
        signals = disputed_fact_signals(disputed, ledger)
        if signals:
            audit.record("belief_uncertainty_flagged", count=len(signals),
                         entities=[s.entity for s in signals])
        return signals

    def _semantic_memory(self) -> SemanticMemory | None:
        """The single shared SemanticMemory instance (never a second one over
        the same semantic.json -- see __init__: parallel instances clobber
        each other's persisted facts)."""
        if self.memory_mcp is not None and hasattr(self.memory_mcp, "memory"):
            return self.memory_mcp.memory
        if self.state_dir is None:
            return None
        return SemanticMemory(
            self.state_dir,
            require_evidence=self.policy.semantic_promotion_requires_evidence,
        )

    def _persist_supplier_graph(self, ledger: EvidenceLedger) -> None:
        if self.state_dir is None:
            return
        target = self.state_dir / "graphs" / f"{ledger.run_id}.mmd"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_supplier_graph(ledger), encoding="utf-8")

    def _persist_semantic(self, validated, run_id: str, review_store: ReviewStore | None) -> None:
        if self.state_dir is None:
            return
        memory = self._semantic_memory()
        if memory is None:
            return
        for cand in validated:
            if isinstance(cand, ContactCandidate):
                domain_match = bool(cand.validation_signals.get("domain_match"))
                for contact in cand.contacts:
                    # v1 promotion is per extracted contact. Cross-source promotion
                    # becomes active when candidate construction aggregates same-value
                    # contacts across multiple fetched pages.
                    if not should_promote_contact(
                        evidence_refs=[contact.evidence_ref],
                        confidence=contact.confidence,
                        domain_match=domain_match,
                    ):
                        continue
                    stored = memory.upsert(
                        SemanticFact(
                            entity_type="vendor",
                            entity_name=cand.vendor_name,
                            supplier_id=cand.supplier_id,
                            field=f"contact_{contact.type}",
                            value=contact.value,
                            confidence=contact.confidence,
                            privacy_class=contact.privacy_class,
                            evidence_refs=[contact.evidence_ref],
                        )
                    )
                    self._maybe_review_disputed_fact(run_id, review_store, stored)
            if isinstance(cand, ServiceCandidate) and cand.quote_channel is not None:
                stored = memory.upsert(
                    SemanticFact(
                        entity_type="vendor",
                        entity_name=cand.vendor_name,
                        supplier_id=cand.supplier_id,
                        field="quote_channel",
                        value=cand.quote_channel.value,
                        confidence=0.85,
                        evidence_refs=[cand.quote_channel.evidence_ref],
                    )
                )
                self._maybe_review_disputed_fact(run_id, review_store, stored)

    def _maybe_review_disputed_fact(
        self,
        run_id: str,
        review_store: ReviewStore | None,
        fact: SemanticFact,
    ) -> None:
        if review_store and fact.status == "disputed":
            review_store.create(
                run_id=run_id,
                reason="disputed fact promotion",
                proposed_action=f"review semantic fact {fact.fact_id}",
                detail=fact.model_dump(mode="json"),
            )


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _merge_refs(*groups) -> list:
    seen: set[str] = set()
    out = []
    for group in groups:
        for ref in group or []:
            if ref is None or ref.ledger_id in seen:
                continue
            seen.add(ref.ledger_id)
            out.append(ref)
    return out


def _span_for_match(text: str, matched_text: str, fallback_terms: list[str] | None = None) -> tuple[str, int, int, str]:
    body = text or ""
    target = (matched_text or "").strip()
    lower = body.lower()
    needle = target.lower()
    if needle and needle in lower:
        span_start = lower.index(needle)
        span_end = span_start + len(target)
        snippet_start = max(0, span_start - 180)
        snippet_end = min(len(body), span_end + 180)
        return body[snippet_start:snippet_end].strip(), span_start, span_end, body[span_start:span_end]
    for term in fallback_terms or []:
        needle = term.lower()
        if needle and needle in lower:
            span_start = lower.index(needle)
            span_end = span_start + len(term)
            snippet_start = max(0, span_start - 180)
            snippet_end = min(len(body), span_end + 180)
            return body[snippet_start:snippet_end].strip(), span_start, span_end, body[span_start:span_end]
    return target, -1, -1, target


def _same_vendor(a: str, b: str) -> bool:
    left = normalize_vendor_name(a)
    right = normalize_vendor_name(b)
    return bool(left and right and (left in right or right in left))


def _quote_type_from_memory(value: str):
    if "@" in value:
        from ..modes.contracts import QuoteChannelType

        return QuoteChannelType.CONTACT_EMAIL
    if re.search(r"\d{7,}", re.sub(r"\D", "", value or "")):
        from ..modes.contracts import QuoteChannelType

        return QuoteChannelType.PHONE
    from ..modes.contracts import QuoteChannelType

    return QuoteChannelType.CONTACT_PAGE
