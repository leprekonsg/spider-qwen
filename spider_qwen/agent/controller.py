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

from .budget import BudgetExceeded, BudgetTracker, StopReason
from .execution_context import ExecutionContext, new_run_id
from .planner import Planner
from .policy import Policy, load_policy
from ..api.schema import Classification, RunResult
from ..evidence.ledger import EvidenceLedger
from ..extraction.contact import ContactExtractor
from ..extraction.dedupe import dedupe_candidates
from ..extraction.pricing import PricingExtractor
from ..extraction.quote_channel import QuoteChannelExtractor
from ..extraction.service_match import ServiceMatchExtractor
from ..extraction.vendor_metadata import VendorMetadataExtractor
from ..governance.audit import AuditLog
from ..memory.episodic import EpisodicMemory, EpisodicRecord
from ..memory.promotion import should_promote_contact
from ..memory.semantic import SemanticFact, SemanticMemory
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
from ..modes.router import ModeRouter, RoutePlan
from ..observability.metrics import Metrics
from ..observability.tracing import Tracer
from ..ranking.contact_ranker import ContactRanker
from ..ranking.geo_strategy import SEA_COUNTRIES, GeoStrategy, build_query_templates
from ..ranking.product_ranker import ProductRanker
from ..ranking.service_ranker import ServiceRanker
from ..rfq.generator import RFQGenerator
from ..tools.fetch_service import FetchService, build_fetch_provider
from ..tools.search_service import SearchService, build_search_provider

_MOQ_RE = re.compile(r"(?:MOQ|minimum order(?: quantity)?)\D{0,15}([\d,]+)", re.IGNORECASE)
_EVIDENCE_SOURCE_TOOLS = {"tinyfish_search", "tinyfish_fetch", "qwen_web_extractor", "mcp_search", "mock"}
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
        state_dir: str | Path | None = None,
        persist: bool = True,
    ) -> None:
        self.policy = policy or load_policy()
        self.search_provider = search_provider or build_search_provider()
        self.fetch_provider = fetch_provider or build_fetch_provider()
        self.state_dir = Path(state_dir) if state_dir else None
        self.persist = persist and self.state_dir is not None
        self.classifier = ModeClassifier()
        self.router = ModeRouter()
        self.planner = Planner()
        self.geo = GeoStrategy(self.policy.boost_countries, self.policy.default_region)
        self._extractors = {
            "vendor_metadata": VendorMetadataExtractor(),
            "pricing": PricingExtractor(),
            "contact": ContactExtractor(),
            "quote_channel": QuoteChannelExtractor(),
            "service_match": ServiceMatchExtractor(),
        }
        self._rankers = {"product": ProductRanker(), "service": ServiceRanker(), "contact": ContactRanker()}

    async def run(self, query: str, mode: str = "auto", target_country: str | None = None) -> RunResult:
        classification = self.classifier.classify(query, forced_mode=mode)
        chosen = classification.mode
        route = self.router.route(chosen)
        budget = self.policy.budget_for(chosen, route.budget_key)

        run_id = new_run_id()
        ledger = EvidenceLedger(run_id, self.state_dir)
        tracker = BudgetTracker(budget)
        working = WorkingMemory(run_id=run_id, query=query, mode=chosen.value)
        tracer = Tracer(run_id, chosen.value, self.state_dir)
        audit = AuditLog(run_id, self.state_dir)
        metrics = Metrics()
        ctx = ExecutionContext(
            run_id=run_id, query=query, mode=chosen, ledger=ledger,
            tracker=tracker, working=working, tracer=tracer,
        )

        search = SearchService(self.search_provider, ledger, tracker, tracer)
        fetch = FetchService(self.fetch_provider, ledger, tracker, tracer)

        if target_country is None:
            target_country = self._detect_target_country(query)

        # SEA-first gather, then global fallback only if min not met.
        candidates = await self._gather(
            ctx, route, query, search, fetch, region="SEA", target_country=target_country,
            reserve_search_calls=1 if budget.max_search_calls > 1 else 0,
        )
        ranker = self._rankers[route.ranker]
        ranked = ranker.rank(candidates)
        validated = [c for c in ranked if self._is_validated(c, chosen, budget)]

        extraction_budget_remaining = tracker.candidates_extracted < budget.max_candidates_to_extract
        if (
            len(validated) < budget.min_validated_candidates
            and extraction_budget_remaining
            and tracker.can_search()
            and not tracker.runtime_exceeded()
        ):
            tracer.record(step="geo_fallback", tool="search", status="success")
            more = await self._gather(
                ctx, route, query, search, fetch, region="global", target_country=target_country
            )
            candidates = dedupe_candidates(candidates + more)
            ranked = ranker.rank(candidates)
            validated = [c for c in ranked if self._is_validated(c, chosen, budget)]

        validated = validated[: budget.max_validated_candidates]
        stop_reason = self._stop_reason(chosen, validated, candidates, tracker, budget)

        rfq_drafts: list[dict] = []
        if route.produces_rfq:
            rfq_drafts = self._build_rfqs(query, validated, target_country, metrics, audit)

        metrics.search_calls_total = tracker.search_calls
        metrics.fetch_urls_total = tracker.fetch_urls
        metrics.validated_candidates_total = len(validated)
        metrics.candidates_considered = len(candidates)
        metrics.quote_channel_found = sum(
            1 for c in candidates if isinstance(c, ServiceCandidate) and c.quote_channel is not None
        )
        metrics.avg_runtime_seconds = round(tracker.elapsed_seconds(), 3)
        metrics.budget_exhausted = tracker.stop_reason is not None

        result = RunResult(
            run_id=run_id,
            query=query,
            mode=chosen.value,
            stop_reason=stop_reason.value,
            classification=Classification(
                mode=chosen.value, confidence=classification.confidence, rationale=classification.rationale
            ),
            validated_candidates=[c.model_dump(mode="json") for c in validated],
            pricing_status_summary=self._pricing_summary(candidates),
            rfq_drafts=rfq_drafts,
            evidence_refs=[c_ref for c in validated for c_ref in c.evidence_refs],
            metrics={
                **metrics.model_dump(),
                "quote_channel_found_rate": metrics.quote_channel_found_rate,
            },
            budget=tracker.snapshot(),
        )

        self._persist_run(ctx, audit, result, validated)
        return result

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
    ) -> list:
        templates = build_query_templates(
            query, region=region, target_country=target_country, mode=route.mode.value
        )
        location = None if region == "global" else self.geo.location_code(target_country)
        urls: list[str] = []
        for template in templates:
            if not ctx.tracker.can_search():
                break
            if reserve_search_calls and ctx.tracker.remaining_search_calls() <= reserve_search_calls:
                break
            try:
                result_set = await search.search(template, location=location)
            except BudgetExceeded:
                break
            urls.extend(result_set.urls())
        urls = _dedupe(urls)[: ctx.tracker.budget.max_candidates_to_extract]
        ctx.working.add_urls(urls)
        if not urls or not ctx.tracker.can_fetch():
            return []

        try:
            fetched = await fetch.fetch(urls)
        except BudgetExceeded:
            return []
        ctx.working.add_fetched([p.final_url or p.url for p in fetched.results])

        candidates = []
        for page in fetched.results:
            if not page.text:
                continue
            if not ctx.tracker.consume_extraction():
                break
            candidates.append(self._build_candidate(ctx, route, query, page, target_country))
        return candidates

    def _build_candidate(self, ctx: ExecutionContext, route: RoutePlan, query: str, page, target_country: str | None):
        meta = self._extractors["vendor_metadata"].extract(
            page_url=page.url, final_url=page.final_url, title=page.title, text=page.text
        )
        ref = page.evidence_ref
        refs = [ref] if ref else []
        geo_score = self.geo.score(meta.country, target_country, page.text)
        page_url = page.final_url or page.url

        if route.ranker == "product":
            return self._product_candidate(ctx, query, page, meta, refs, geo_score, page_url)
        if route.ranker == "service":
            return self._service_candidate(ctx, query, page, meta, refs, geo_score, page_url, target_country)
        return self._contact_candidate(ctx, page, meta, refs, geo_score)

    def _product_candidate(self, ctx, query, page, meta, refs, geo_score, page_url):
        pricing = self._extractors["pricing"].extract(page.text)
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

    def _service_candidate(self, ctx, query, page, meta, refs, geo_score, page_url, target_country):
        sm = self._extractors["service_match"].extract(query, page.text)
        service_ref = self._record_extraction_ref(
            ctx, page, "service_match", "", fallback_terms=sm.matched_terms
        ) if sm.matched else None
        qc_matches = self._extractors["quote_channel"].extract(page.text, page.links, page_url)
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

    def _contact_candidate(self, ctx, page, meta, refs, geo_score):
        matches = self._extractors["contact"].extract(page.text, page.links)
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

    # --- validation / stop ------------------------------------------------
    def _is_validated(self, candidate, mode: ProcurementMode, budget) -> bool:
        if not candidate.has_evidence():
            return False
        if candidate.evidence_completeness < budget.evidence_completeness_threshold:
            return False
        if mode == ProcurementMode.PRODUCT_EXACT_PRICE:
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

    def _build_rfqs(self, query, validated, target_country, metrics: Metrics, audit: AuditLog) -> list[dict]:
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
            drafts.append(draft.model_dump(mode="json"))
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
        snippet = _snippet_for_match(page.text, matched_text, fallback_terms)
        if not snippet:
            snippet = matched_text.strip()
        if not snippet:
            return None
        source_tool = getattr(page, "source_tool", "tinyfish_fetch")
        if source_tool not in _EVIDENCE_SOURCE_TOOLS:
            source_tool = "tinyfish_fetch"
        return ctx.ledger.record(
            source_tool=source_tool,
            url=page.url,
            final_url=page.final_url,
            title=page.title,
            snippet=snippet,
            language=page.language,
            confidence=0.75,
            metadata={
                "extraction": extraction,
                "matched_text": matched_text,
                "parent_ledger_id": page.evidence_ref.ledger_id,
            },
        )

    def _detect_target_country(self, query: str) -> str | None:
        lower = query.lower()
        for country in SEA_COUNTRIES:
            if country.lower() in lower:
                return country
        return None

    def _persist_run(self, ctx: ExecutionContext, audit: AuditLog, result: RunResult, validated) -> None:
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
            self._persist_semantic(validated)
            ctx.ledger.persist()
            ctx.tracer.persist() if ctx.tracer else None
            audit.persist()

    def _persist_semantic(self, validated) -> None:
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
                    memory.upsert(
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
            if isinstance(cand, ServiceCandidate) and cand.quote_channel is not None:
                memory.upsert(
                    SemanticFact(
                        entity_type="vendor",
                        entity_name=cand.vendor_name,
                        field="quote_channel",
                        value=cand.quote_channel.value,
                        confidence=0.85,
                        evidence_refs=[cand.quote_channel.evidence_ref],
                    )
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


def _snippet_for_match(text: str, matched_text: str, fallback_terms: list[str] | None = None) -> str:
    body = text or ""
    target = (matched_text or "").strip()
    lower = body.lower()
    needle = target.lower()
    if needle and needle in lower:
        start = max(0, lower.index(needle) - 180)
        end = min(len(body), lower.index(needle) + len(target) + 180)
        return body[start:end].strip()
    for term in fallback_terms or []:
        needle = term.lower()
        if needle and needle in lower:
            start = max(0, lower.index(needle) - 180)
            end = min(len(body), lower.index(needle) + len(term) + 180)
            return body[start:end].strip()
    return target
