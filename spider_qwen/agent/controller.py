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
from urllib.parse import urlparse

from .budget import Budget, BudgetExceeded, BudgetTracker, StopReason
from .compiler import LLMCompiler, NullRateLimiter, RateLimiter, ToolNode
from .execution_context import ExecutionContext, new_run_id
from .planner import Planner
from .policy import Policy, load_policy
from ..api.schema import Classification, RunResult
from ..evidence.ledger import EvidenceLedger
from ..evidence.graph import render_supplier_graph
from ..evidence.models import EvidenceRef, sha256_hex
from ..evidence.verifier import VerificationSpine
from ..verification.minicheck import MiniCheck
from ..extraction.contact import ContactExtractor
from ..extraction.dedupe import dedupe_candidates
from ..extraction.pricing import PricingExtractor, PricingResult
from ..extraction.quote_channel import QuoteChannelExtractor, QuoteChannelMatch
from ..extraction.service_match import ServiceMatchExtractor
from ..extraction.vendor_metadata import VendorMetadataExtractor
from ..governance.audit import AuditLog
from ..governance.review_events import ReviewStore
from ..memory.episodic import EpisodicMemory, EpisodicRecord
from ..memory.mcp import SemanticMemoryMcpAdapter
from ..memory.promotion import should_promote_contact
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
from ..observability.tracing import Tracer
from ..ranking.contact_ranker import ContactRanker
from ..ranking.geo_strategy import SEA_COUNTRIES, GeoStrategy, build_query_templates
from ..ranking.product_ranker import ProductRanker
from ..ranking.serendipity import build_serendipity_result
from ..ranking.service_ranker import ServiceRanker
from ..serendipity.corrective import corrective_queries, evaluate_retrieval
from ..serendipity.query_rewrite import merge_gather_queries
from ..rfq.generator import RFQGenerator
from ..tools.fetch_service import FetchService, build_fetch_provider
from ..tools.page_judge import PageJudge
from ..tools.qwen_json_extractor import QwenJsonExtractor, QwenPageExtraction
from ..tools.search_service import SearchService, build_search_provider

_MOQ_RE = re.compile(r"(?:MOQ|minimum order(?: quantity)?)\D{0,15}([\d,]+)", re.IGNORECASE)
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
    if not url:
        return ""
    host = urlparse(url).netloc.lower() or url.lower()
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


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
        qwen_router: object | None = None,
        memory_mcp: SemanticMemoryMcpAdapter | None = None,
        state_dir: str | Path | None = None,
        persist: bool = True,
        require_review: bool | None = None,
    ) -> None:
        self.policy = policy or load_policy()
        self.search_provider = search_provider or build_search_provider()
        self.fetch_provider = fetch_provider or build_fetch_provider()
        self.state_dir = Path(state_dir) if state_dir else None
        self.persist = persist and self.state_dir is not None
        self.require_review = self.policy.hitl_require_review() if require_review is None else require_review
        self.classifier = ModeClassifier()
        self.qwen_router = qwen_router
        if self.qwen_router is None and self.policy.qwen_router_fallback_enabled():
            self.qwen_router = QwenModeRouter(model=self.policy.qwen_router_model())
        self.qwen_json_extractor = qwen_json_extractor
        if self.qwen_json_extractor is None and self.policy.qwen_structured_extraction_enabled():
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
        self.memory_mcp = memory_mcp
        if self.memory_mcp is None and self.state_dir is not None:
            self.memory_mcp = SemanticMemoryMcpAdapter(self.state_dir)
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
                            reliability=item.reliability)
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
                  high_risk: bool = False, serendipity: bool = False) -> RunResult:
        classification = self._classify(query, forced_mode=mode)
        chosen = classification.mode
        route = self.router.route(chosen)
        budget = self.policy.budget_for(chosen, route.budget_key)

        run_id = new_run_id()
        ledger = EvidenceLedger(run_id, self.state_dir,
                                reliability_priors=self.policy.source_reliability())
        tracker = BudgetTracker(budget)
        working = WorkingMemory(run_id=run_id, query=query, mode=chosen.value)
        tracer = Tracer(run_id, chosen.value, self.state_dir)
        audit = AuditLog(run_id, self.state_dir)
        review_store = ReviewStore(self.state_dir) if self.persist and self.policy.hitl_enabled() else None
        metrics = Metrics()
        ctx = ExecutionContext(
            run_id=run_id, query=query, mode=chosen, ledger=ledger,
            tracker=tracker, working=working, tracer=tracer,
        )

        search = SearchService(self.search_provider, ledger, tracker, tracer)
        fetch = FetchService(self.fetch_provider, ledger, tracker, tracer,
                             judge=self.page_judge, query=query)
        memory_recalls = self._recall_memory(query, ctx, audit)

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
        candidates = await self._gather(
            ctx, route, query, search, fetch, region="SEA", target_country=target_country,
            reserve_search_calls=1 if budget.max_search_calls > 1 else 0, pages_out=sea_pages,
        )
        candidates = self._apply_memory_recalls(ctx, candidates, memory_recalls)
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

        extraction_budget_remaining = tracker.candidates_extracted < budget.max_candidates_to_extract
        if (
            len(validated) < budget.min_validated_candidates
            and extraction_budget_remaining
            and tracker.can_search()
            and not tracker.runtime_exceeded()
        ):
            if crag.verdict == "incorrect" and crag.assessments:
                # Retrieval judged off-target: broaden / broker-pivot rather than answer.
                corr = corrective_queries(query, crag, mode=chosen.value)
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
            candidates = dedupe_candidates(candidates + more)
            candidates = self._apply_memory_recalls(ctx, candidates, memory_recalls)
            ranked = ranker.rank(candidates)
            validated = [c for c in ranked if self._is_validated(c, chosen, budget)]

        validated = validated[: budget.max_validated_candidates]

        # T-2.2: verification spine. Block candidates whose critical claims are not
        # entailed by their cited evidence; write verified/verifier_score onto the
        # claim ledger rows. Opt-in, so the default offline pipeline is unchanged.
        verification_metrics = {"claims_verified": 0, "claims_unsupported": 0,
                                "candidates_blocked_unverified": 0}
        if self.verify_claims:
            validated, verification_metrics = self._verify_candidates(ledger, validated, tracer)

        stop_reason = self._stop_reason(chosen, validated, candidates, tracker, budget)

        # T-1.1: reshape the ranked candidates into the four-slot serendipity view.
        serendipity_result = build_serendipity_result(ranked, mode=chosen.value)

        rfq_drafts: list[dict] = []
        if route.produces_rfq:
            rfq_drafts = self._build_rfqs(query, validated, target_country, metrics, audit, run_id, review_store)

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
            validated_candidates=[c.model_dump(mode="json") for c in validated],
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
                "crag_verdict": crag.verdict,
                "crag_confidence": crag.confidence,
                "corrective_searches": corrective_searches,
                "pages_rejected": fetch.rejected,
                "pages_flagged": fetch.flagged,
                **verification_metrics,
            },
            budget=tracker.snapshot(),
        )

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
        tracer = Tracer(run_id, chosen.value, self.state_dir)
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
                                 judge=self.page_judge, query=query)
            cands = await self._gather_queries(
                ctx, route, traj.queries, search, fetch, location=None, target_country=target_country,
            )
            ranked = ranker.rank(cands)
            metrics, refs, disputed, conflict = self._bundle_metrics(chosen.value, ranked or cands)
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
            )

        return await TrajectoryRunner(budget=rbudget).run(query, chosen.value, executor=executor)

    def _bundle_metrics(self, mode: str, candidates: list):
        """Map ranked candidates -> normalized PPRM BundleMetrics + evidence refs."""
        from ..reasoning.trajectory import BundleMetrics

        refs: list[EvidenceRef] = []
        seen: set[str] = set()
        for cand in candidates:
            for ref in cand.evidence_refs:
                if ref.ledger_id not in seen:
                    seen.add(ref.ledger_id)
                    refs.append(ref)
        hosts = {urlparse(r.url).netloc for r in refs if r.url}
        diversity = round(min(1.0, len(hosts) / 3.0), 4) if refs else 0.0
        metrics = BundleMetrics(evidence_diversity=diversity)
        disputed, conflict = 0, 0.0

        if mode in {"service_quote_required", "contact_enrichment_only"}:
            svc = [c for c in candidates if isinstance(c, ServiceCandidate)]
            if svc:
                metrics.service_match = round(min(1.0, max(c.service_match_score for c in svc)), 4)
                metrics.quote_channel = 1.0 if any(c.quote_channel for c in svc) else 0.0
                metrics.geo = round(min(1.0, max(c.geo_score for c in svc)), 4)
                metrics.checklist = round(min(1.0, max(c.checklist_completeness for c in svc)), 4)
                metrics.contact_reliability = round(min(1.0, max(c.evidence_completeness for c in svc)), 4)
                conflict = round(max((c.conflict_penalty for c in svc), default=0.0), 4)
                disputed = sum(1 for c in svc if c.conflict_penalty > 0)
        else:
            prod = [c for c in candidates if isinstance(c, ProductCandidate)]
            if prod:
                metrics.fff_similarity = round(min(1.0, max((c.score for c in prod), default=0.0)), 4)
                metrics.authorized_source = round(min(1.0, max(c.geo_score for c in prod)), 4)
                metrics.stock = 1.0 if any(c.pricing_status != PricingStatus.NOT_FOUND for c in prod) else 0.0
                metrics.datasheet_evidence = diversity
                # Known limit (disclosed): the remaining two electronics PPRM dims --
                # lifecycle_safety (0.20) and risk (0.10) -- need PER-SUBSTITUTE
                # lifecycle/FFF/counterfeit signals from the T-5.x miners, which are a
                # v2 deferral. A blunt detect_lifecycle over the mixed evidence here
                # would return the OBSOLETE original's state and wrongly penalise the
                # very substitute-finding trajectories the mode targets, so we leave
                # both unset (default 0.0) rather than fabricate a score. This caps a
                # perfect electronics bundle at ~0.7 by design until the miners land.
        return metrics, refs, disputed, conflict

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
    ) -> list:
        templates = build_query_templates(
            query, region=region, target_country=target_country, mode=route.mode.value
        )
        expanded = self.planner.expand_query(query, mode=route.mode.value)
        max_queries = ctx.tracker.remaining_search_calls()
        if reserve_search_calls:
            max_queries = max(0, max_queries - reserve_search_calls)
        queries = merge_gather_queries(templates, expanded, max_queries=max_queries)
        ctx.tracer.record(
            step="query_expand", tool="query_rewrite", status="success",
            input_count=1, output_count=len(queries),
            detail={"region": region, "kinds": sorted({sq.kind for sq in expanded}),
                    "queries": queries[:12]},
        )
        location = None if region == "global" else self.geo.location_code(target_country)
        return await self._gather_queries(
            ctx, route, queries, search, fetch,
            location=location, target_country=target_country,
            reserve_search_calls=reserve_search_calls, pages_out=pages_out,
        )

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
        urls = await self._collect_search_urls(
            ctx, queries, search, location=location, reserve_search_calls=reserve_search_calls,
        )
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
        urls = await self._collect_search_urls(ctx, queries, search, location=location)
        return await self._fetch_and_extract(ctx, route, urls, fetch, target_country, pages_out)

    async def _collect_search_urls(
        self,
        ctx: ExecutionContext,
        queries: list[str],
        search: SearchService,
        *,
        location: str | None,
        reserve_search_calls: int = 0,
    ) -> list[str]:
        """Run one or many search queries; return deduped URLs in discovery order."""
        max_searches = ctx.tracker.remaining_search_calls()
        if reserve_search_calls:
            max_searches = max(0, max_searches - reserve_search_calls)
        budgeted = list(queries)[:max_searches]
        if not budgeted:
            return []

        urls: list[str] = []
        seen: set[str] = set()

        def _add(result_set) -> None:
            if result_set is None:
                return
            for u in result_set.urls():
                if u not in seen:
                    seen.add(u)
                    urls.append(u)

        if len(budgeted) == 1:
            if ctx.tracker.can_search():
                try:
                    _add(await search.search(budgeted[0], location=location))
                except BudgetExceeded:
                    pass
            return urls

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
        return urls

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
            candidates.append(self._build_candidate(ctx, route, ctx.query, page, target_country))
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
                if not ctx.tracker.can_fetch():
                    return None
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

    def _recall_memory(self, query: str, ctx: ExecutionContext, audit: AuditLog) -> list[MemoryRecall]:
        if self.memory_mcp is None:
            return []
        try:
            if hasattr(self.memory_mcp, "memory"):
                self.memory_mcp.memory.maintain()
            recalls = self.memory_mcp.recall(query=query, top_k=5, context_budget_chars=1200)
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

    def _build_candidate(self, ctx: ExecutionContext, route: RoutePlan, query: str, page, target_country: str | None):
        meta = self._extractors["vendor_metadata"].extract(
            page_url=page.url, final_url=page.final_url, title=page.title, text=page.text
        )
        ref = page.evidence_ref
        refs = [ref] if ref else []
        geo_score = self.geo.score(meta.country, target_country, page.text)
        page_url = page.final_url or page.url
        qwen = self._extract_qwen_json(ctx, query, page_url, page.text)

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
        return result

    def _product_candidate(self, ctx, query, page, meta, refs, geo_score, page_url, qwen: QwenPageExtraction | None):
        pricing = self._extractors["pricing"].extract(page.text)
        if qwen and qwen.pricing.status != PricingStatus.NOT_FOUND:
            pricing = PricingResult(
                status=qwen.pricing.status,
                price=qwen.pricing.price,
                currency=qwen.pricing.currency,
                unit=qwen.pricing.unit,
                matched_text=qwen.pricing.matched_text,
            )
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
            qc_matches.extend(
                QuoteChannelMatch(type=q.type, value=q.value, matched_text=q.matched_text or q.value)
                for q in qwen.quote_channels
            )
        best = self._extractors["quote_channel"].best(qc_matches)
        quote_channel = None
        quote_ref = None
        if best and refs:
            quote_ref = self._record_extraction_ref(
                ctx, page, "quote_channel", best.value or best.matched_text
            )
            quote_channel = QuoteChannel(type=best.type, value=best.value, evidence_ref=quote_ref or refs[0])
        pricing = self._extractors["pricing"].extract(page.text)
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
    def _verify_candidates(self, ledger: EvidenceLedger, validated, tracer):
        """Verify each candidate's claims; drop those with unsupported critical claims."""
        spine = VerificationSpine(ledger, minicheck=self.minicheck or MiniCheck())
        kept = []
        verified_claims = unsupported_claims = blocked = 0
        for cand in validated:
            cv = spine.verify_candidate(cand)
            verified_claims += sum(1 for c in cv.claims if c.verified)
            unsupported_claims += sum(1 for c in cv.claims if not c.verified)
            if not cv.verified:
                blocked += 1
                if tracer is not None:
                    tracer.record(step="verify_claims", tool="minicheck_verifier", status="blocked",
                                  input_count=len(cv.claims), output_count=0,
                                  detail=cv.model_dump(mode="json"))
                continue
            if tracer is not None:
                tracer.record(step="verify_claims", tool="minicheck_verifier", status="success",
                              input_count=len(cv.claims), output_count=len(cv.claims),
                              detail={"vendor": cv.vendor_name, "verifier_score": cv.verifier_score})
            kept.append(cand)
        metrics = {"claims_verified": verified_claims, "claims_unsupported": unsupported_claims,
                   "candidates_blocked_unverified": blocked}
        return kept, metrics

    # --- validation / stop ------------------------------------------------
    def _is_validated(self, candidate, mode: ProcurementMode, budget) -> bool:
        if not candidate.has_evidence():
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
    ) -> list[dict]:
        generator = RFQGenerator(
            tone=self.policy.rfq_tone,
            minimum_completeness=self.policy.minimum_checklist_completeness,
        )
        drafts: list[dict] = []
        for cand in validated:
            if not isinstance(cand, ServiceCandidate):
                continue
            draft = generator.generate(query=query, candidate=cand, target_country=target_country)
            metrics.rfq_drafts_total += 1
            if draft.status == "incomplete":
                metrics.rfq_incomplete_total += 1
            audit.record("rfq_draft_generated", vendor=cand.vendor_name, status=draft.status)
            draft_dict = draft.model_dump(mode="json")
            if review_store and self.require_review:
                # Blocking checkpoint: withhold the polished RFQ until a human
                # approves. The full draft is carried in the review event detail
                # and released by `review approve <event_id>`.
                event = review_store.create(
                    run_id=run_id,
                    reason="rfq finalization",
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
    def _pricing_summary(self, candidates) -> dict[str, int]:
        summary: dict[str, int] = {}
        for c in candidates:
            status = getattr(c, "pricing_status", None)
            if status is not None:
                summary[status.value] = summary.get(status.value, 0) + 1
        return summary

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
            self._persist_supplier_graph(ctx.ledger)
            ctx.tracer.persist() if ctx.tracer else None
            audit.persist()

    def _persist_supplier_graph(self, ledger: EvidenceLedger) -> None:
        if self.state_dir is None:
            return
        target = self.state_dir / "graphs" / f"{ledger.run_id}.mmd"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_supplier_graph(ledger), encoding="utf-8")

    def _persist_semantic(self, validated, run_id: str, review_store: ReviewStore | None) -> None:
        if self.state_dir is None:
            return
        memory = SemanticMemory(
            self.state_dir,
            require_evidence=self.policy.semantic_promotion_requires_evidence,
        )
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
    left = _normal_vendor(a)
    right = _normal_vendor(b)
    return bool(left and right and (left in right or right in left))


def _normal_vendor(name: str) -> str:
    stop = {"pte", "ltd", "sdn", "bhd", "llc", "inc", "co", "company", "team"}
    return " ".join(t for t in re.sub(r"[^a-z0-9 ]", " ", (name or "").lower()).split() if t not in stop)


def _quote_type_from_memory(value: str):
    if "@" in value:
        from ..modes.contracts import QuoteChannelType

        return QuoteChannelType.CONTACT_EMAIL
    if re.search(r"\d{7,}", re.sub(r"\D", "", value or "")):
        from ..modes.contracts import QuoteChannelType

        return QuoteChannelType.PHONE
    from ..modes.contracts import QuoteChannelType

    return QuoteChannelType.CONTACT_PAGE
