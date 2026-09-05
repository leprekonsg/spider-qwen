"""Span-level evidence verification + the T-2.2 claim verification spine.

``verify_ledger`` checks the structural integrity of recorded spans (offsets,
hashes). ``VerificationSpine`` is the trust gatekeeper: it decomposes a candidate
into atomic claims, checks each against its cited evidence (MiniCheck), and
re-grounds flagged atoms against the wider corpus (SAFE) before marking each
claim ``verified`` and blocking candidates whose critical claims do not hold.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .ledger import EvidenceLedger
from .models import sha256_hex
from ..verification.atomic import AtomicClaim, decompose
from ..verification.grade import grade_claim, worst_grade
from ..verification.grounding import classify_grounding, worst_decision
from ..verification.minicheck import HARD_REJECTION_METHODS, MiniCheck
from ..verification.safe import SafeReverifier


class EvidenceVerificationIssue(BaseModel):
    ledger_id: str
    reason: str


class EvidenceVerificationResult(BaseModel):
    run_id: str
    checked_claims: int = 0
    valid_claims: int = 0
    issues: list[EvidenceVerificationIssue] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues


def verify_ledger(ledger: EvidenceLedger) -> EvidenceVerificationResult:
    result = EvidenceVerificationResult(run_id=ledger.run_id)
    for item in ledger.items():
        parent_id = item.metadata.get("parent_ledger_id")
        start = item.metadata.get("start_char")
        end = item.metadata.get("end_char")
        span_hash = item.metadata.get("span_hash")
        if start is None and end is None:
            continue
        result.checked_claims += 1
        if not parent_id:
            result.issues.append(_issue(item.ledger_id, "missing parent_ledger_id"))
            continue
        parent = ledger.get(parent_id)
        if parent is None:
            result.issues.append(_issue(item.ledger_id, f"missing parent evidence {parent_id}"))
            continue
        if parent.text is None:
            result.issues.append(_issue(item.ledger_id, f"parent evidence {parent_id} has no stored text"))
            continue
        if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end <= start:
            # end == start is a zero-length span: "" is vacuously inside every
            # snippet and sha256("") matches a degenerate span_hash. Reject it.
            result.issues.append(_issue(item.ledger_id, "invalid span offsets"))
            continue
        span = parent.text[start:end]
        if sha256_hex(span) != span_hash:
            result.issues.append(_issue(item.ledger_id, "span_hash mismatch"))
            continue
        if sha256_hex(item.snippet) != item.snippet_hash:
            result.issues.append(_issue(item.ledger_id, "snippet_hash mismatch"))
            continue
        if item.snippet.strip() and item.snippet.strip() not in span and span not in item.snippet:
            result.issues.append(_issue(item.ledger_id, "snippet does not align with parent span"))
            continue
        result.valid_claims += 1
    return result


def _issue(ledger_id: str, reason: str) -> EvidenceVerificationIssue:
    return EvidenceVerificationIssue(ledger_id=ledger_id, reason=reason)


# --- T-2.2: claim verification spine --------------------------------------

# Which extraction ledger row backs each claim field (metadata["field"] tag).
_FIELD_TAG = {"price": "pricing", "service_match": "service_match", "quote_channel": "quote_channel"}

# Vendor-scoped claims that require subject+value co-occurrence (not value-only).
_RELATION_FIELDS = frozenset({"price", "moq", "quote_channel"})


class ClaimVerification(BaseModel):
    claim_id: str
    field: str
    subject: str
    predicate: str
    verified: bool
    verifier_score: float
    critical: bool
    stage: str  # minicheck | minicheck+safe
    rationale: str = ""
    # GSAR typed grounding: grounded | complementary | contradicted | ungrounded
    grounding: str = ""
    decision: str = ""  # proceed | regenerate | replan
    grade: str = ""  # GRADE: high | moderate | low | very_low
    # Complementary repair: the ledger row whose page actually grounds the
    # claim, appended to the candidate's citations before emission.
    repointed_ledger_id: str = ""


class CandidateVerification(BaseModel):
    vendor_name: str
    verified: bool  # every critical claim holds
    # min score over CRITICAL claims (all claims when none are critical): the
    # scalar the statistical emission gate reads must not mix in non-critical
    # claim strength, or a fuzzy optional atom can block a flawless candidate.
    verifier_score: float
    claims: list[ClaimVerification] = Field(default_factory=list)
    unsupported_critical: list[str] = Field(default_factory=list)
    decision: str = "proceed"  # worst per-claim decision over ALL claims (drives replan)
    grade: str = ""  # worst grade among verified claims (very_low when none verify)


class VerificationSpine:
    """Decompose -> MiniCheck -> SAFE, writing verdicts back onto claim rows.

    A claim is grounded against its source *page* text (``parent_ledger_id``),
    never the extraction snippet, so a fabricated value whose snippet is
    self-referential is still caught. Vendor-scoped critical claims require the
    value and vendor to co-occur in the same sentence on that page (and in SAFE
    corpus spans); a competitor's price on the same page does not count. A
    candidate is ``verified`` only when every critical claim holds.
    """

    def __init__(self, ledger: EvidenceLedger, *, minicheck: MiniCheck | None = None,
                 safe: SafeReverifier | None = None, threshold: float = 0.5) -> None:
        self.ledger = ledger
        self.minicheck = minicheck or MiniCheck(threshold=threshold)
        self.safe = safe or SafeReverifier(self.minicheck)

    def verify_candidate(self, candidate: Any) -> CandidateVerification:
        results = [self._verify_claim(candidate, claim) for claim in decompose(candidate)]
        if not results:
            return CandidateVerification(
                vendor_name=getattr(candidate, "vendor_name", "") or "",
                verified=False,
                verifier_score=0.0,
                claims=[],
                unsupported_critical=["no_claims"],
                decision="replan",
                grade="very_low",
            )
        unsupported_critical = [r.claim_id for r in results if r.critical and not r.verified]
        critical = [r for r in results if r.critical]
        score = round(min(r.verifier_score for r in (critical or results)), 4)
        verified_grades = [r.grade for r in results if r.verified and r.grade]
        self._repoint_citations(candidate, results)
        return CandidateVerification(
            vendor_name=getattr(candidate, "vendor_name", "") or "",
            verified=not unsupported_critical, verifier_score=score,
            claims=results, unsupported_critical=unsupported_critical,
            decision=worst_decision([r.decision for r in results]),
            grade=worst_grade(verified_grades) if verified_grades else "very_low",
        )

    def _verify_claim(self, candidate: Any, claim: AtomicClaim) -> ClaimVerification:
        ref = self._ref_for_claim(candidate, claim)
        premise = self._premise_from_ref(ref)
        subject = self._minicheck_subject(claim)
        result = self.minicheck.check(
            claim=claim.predicate, value=claim.object_value, evidence_span=premise,
            field=claim.field, subject=subject, currency=claim.currency,
            unit=claim.unit, pricing_status=claim.pricing_status,
            channel_type=claim.channel_type,
        )
        cited_supported = result.supported
        stage = "minicheck"
        corpus_entries: list[tuple[Any, str]] = []
        if not result.supported:
            corpus_entries = self._corpus_entries(exclude=premise)
            reverified = self.safe.reverify(claim, corpus=[s for _, s in corpus_entries])
            if reverified.score > result.score or (
                result.method == "no_evidence"
                and reverified.method.removeprefix("safe_") in HARD_REJECTION_METHODS
            ):
                result = reverified
            stage = "minicheck+safe"
        grounding = classify_grounding(
            supported_on_cited=cited_supported,
            supported_on_corpus=(not cited_supported) and result.supported,
            subject=subject, value=claim.object_value, cited_span=premise,
        )
        # Complementary repair (CiteFix-style): the corroborating span already
        # passed the full relation gate inside SAFE, so re-pointing does not
        # weaken the check -- it fixes the citation. Claim text is never edited.
        repointed_ledger_id = ""
        if grounding.label == "complementary" and result.winning_span:
            repointed_ledger_id = next(
                (item.ledger_id for item, span in corpus_entries
                 if span == result.winning_span), "")
        graded = grade_claim(
            source_class=self._source_class_for(ref),
            exact_span=self._has_exact_span(ref),
            grounding=grounding.label,
            corroborating_spans=self._corroborations(claim, subject, premise, ref)
            if grounding.label == "grounded" else 1,
            reliability_priors=getattr(self.ledger, "reliability_priors", None),
        )
        verification = ClaimVerification(
            claim_id=claim.claim_id, field=claim.field, subject=claim.subject,
            predicate=claim.predicate, verified=result.supported,
            verifier_score=result.score, critical=claim.critical, stage=stage,
            rationale=result.rationale,
            grounding=grounding.label, decision=grounding.decision,
            grade=graded.grade,
            repointed_ledger_id=repointed_ledger_id,
        )
        self._write_back(ref, verification)
        return verification

    def _repoint_citations(self, candidate: Any, results: list[ClaimVerification]) -> None:
        """Append each complementary claim's corroborating ref to the candidate.

        The emitted citations must include a span that actually supports the
        claim; the original extraction ref is kept as provenance and the claim
        row's ``repointed_to`` annotation records the repair for audit.
        """
        refs = getattr(candidate, "evidence_refs", None)
        if refs is None:
            return
        have = {getattr(r, "ledger_id", "") for r in refs}
        for r in results:
            if r.repointed_ledger_id and r.repointed_ledger_id not in have:
                item = self.ledger.get(r.repointed_ledger_id)
                if item is not None:
                    refs.append(item.to_ref())
                    have.add(r.repointed_ledger_id)

    def _source_class_for(self, ref) -> str:
        """T-2.4 source tier of the cited evidence row (lazy import: cycle)."""
        from ..governance.source_reliability import (
            DEFAULT_RELIABILITY, classify_source, host_of,
        )

        item = self.ledger.get(getattr(ref, "ledger_id", "")) if ref is not None else None
        if item is None:
            return "unknown"
        if item.source_tool == "semantic_memory":
            # A recalled fact's provenance is the ORIGINAL evidence it was
            # promoted from (metadata.source_evidence_refs), not the synthetic
            # recall row -- grading the recall row would erase a manufacturer
            # pedigree down to "unknown". Classification is URL-only here (the
            # original page text lives in a previous run's ledger); take the
            # most reliable class among the original sources.
            sources = item.metadata.get("source_evidence_refs") or []
            classes = [classify_source(host_of(s.get("url", "")))
                       for s in sources if isinstance(s, dict) and s.get("url")]
            if classes:
                table = {**DEFAULT_RELIABILITY,
                         **(getattr(self.ledger, "reliability_priors", None) or {})}
                return max(classes, key=lambda c: table.get(c, 0.0))
            return "unknown"
        # Extraction claim rows carry no page text; classify their source page.
        parent = self.ledger.get(item.metadata.get("parent_ledger_id") or "")
        if parent is not None:
            item = parent
        return classify_source(host_of(item.final_url or item.url),
                               text=item.text or "", title=item.title or "")

    def _has_exact_span(self, ref) -> bool:
        """True when the claim row carries verified, non-degenerate span offsets.

        A zero-length span (start == end) selects the empty string, which is
        vacuously "present" everywhere; it must not earn exact-span credit.
        """
        item = self.ledger.get(getattr(ref, "ledger_id", "")) if ref is not None else None
        if item is None:
            return False
        start = item.metadata.get("start_char")
        end = item.metadata.get("end_char")
        return isinstance(start, int) and isinstance(end, int) and end > start

    def _host_for(self, ref) -> str:
        from ..governance.source_reliability import host_of

        item = self.ledger.get(getattr(ref, "ledger_id", "")) if ref is not None else None
        if item is None:
            return ""
        parent = self.ledger.get(item.metadata.get("parent_ledger_id") or "")
        if parent is not None:
            item = parent
        return host_of(item.final_url or item.url)

    def _corroborations(self, claim: AtomicClaim, subject: str, cited_premise: str, ref) -> int:
        """1 (the cited span) + INDEPENDENT ledger spans that also support it.

        Independence is per registrable host: the vendor's own /pricing and
        /about pages repeating a price are one source, not three -- same
        notion belief.py applies to supporting-source counts. Stops at
        CORROBORATION_UPGRADE: the count only feeds the GRADE +1 threshold.
        """
        from ..governance.source_reliability import host_of
        from ..verification.grade import CORROBORATION_UPGRADE

        count = 1
        seen_hosts = {h for h in (self._host_for(ref),) if h}
        for item, span in self._corpus_entries(exclude=cited_premise):
            if count >= CORROBORATION_UPGRADE:
                break
            host = host_of(item.final_url or item.url)
            if host and host in seen_hosts:
                continue
            check = self.minicheck.check(
                claim=claim.predicate, value=claim.object_value, evidence_span=span,
                field=claim.field, subject=subject, currency=claim.currency,
                unit=claim.unit, pricing_status=claim.pricing_status,
                channel_type=claim.channel_type,
            )
            if check.supported:
                count += 1
                if host:
                    seen_hosts.add(host)
        return count

    @staticmethod
    def _minicheck_subject(claim: AtomicClaim) -> str:
        if claim.field == "vendor_name":
            return ""
        if claim.field in _RELATION_FIELDS or claim.field.startswith("contact_"):
            return claim.subject
        return ""

    def _ref_for_claim(self, candidate: Any, claim: AtomicClaim):
        if claim.evidence_ref is not None:
            return claim.evidence_ref
        refs = getattr(candidate, "evidence_refs", None) or []
        tag = _FIELD_TAG.get(claim.field)
        if tag:
            for ref in refs:
                item = self.ledger.get(ref.ledger_id)
                if item is not None and item.metadata.get("field") == tag:
                    return ref
        # Fall back to the page-level evidence row (full text, no extraction tag).
        for ref in refs:
            item = self.ledger.get(ref.ledger_id)
            if item is not None and not item.metadata.get("field") and item.text:
                return ref
        return refs[0] if refs else None

    def _premise_from_ref(self, ref) -> str:
        item = self.ledger.get(getattr(ref, "ledger_id", "")) if ref is not None else None
        if item is None:
            return ""
        # Recalled/synthetic facts (e.g. semantic_memory) carry the claim value as
        # their own snippet and have no cited page; they must not self-ground. Force
        # them through SAFE corpus re-grounding by returning no premise here.
        if item.source_tool == "semantic_memory":
            return ""
        parent_id = item.metadata.get("parent_ledger_id")
        if parent_id:
            parent = self.ledger.get(parent_id)
            if parent is not None and parent.text:
                # The genuine source page (title + body), not a self-referential snippet.
                return _join(parent.title, parent.text)
        if item.text:
            return _join(item.title, item.text)
        return ""  # no genuine page text -> ungrounded; never fall back to the snippet

    def _corpus_entries(self, *, exclude: str) -> list[tuple[Any, str]]:
        """(ledger item, joined span) per corpus page, so a SAFE verdict can be
        traced back to the row that produced it (re-pointing, host dedupe)."""
        entries: list[tuple[Any, str]] = []
        for item in self.ledger.items():
            if not item.text:
                continue
            span = _join(item.title, item.text)
            if span and span != exclude:
                entries.append((item, span))
        return entries

    def _write_back(self, ref, verification: ClaimVerification) -> None:
        item = self.ledger.get(getattr(ref, "ledger_id", "")) if ref is not None else None
        if item is None or "claim_id" not in item.metadata:
            return  # only annotate genuine claim rows, not page-level evidence
        # annotate() owns the chain consequences: marks it stale for one lazy
        # reseal, and refuses if a tree_head commitment was already published.
        updates = {
            "verified": verification.verified,
            "verifier_score": verification.verifier_score,
            "verifier_stage": verification.stage,
            "grounding": verification.grounding,
            "grade": verification.grade,
        }
        if verification.repointed_ledger_id:
            # Audit trail for the CiteFix-style repair: this row's cited page
            # did not ground the claim; the row named here did.
            updates["repointed_to"] = verification.repointed_ledger_id
        self.ledger.annotate(item.ledger_id, updates)


def _join(title: str | None, text: str) -> str:
    return f"{title}\n{text}" if title else text
