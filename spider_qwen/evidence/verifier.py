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
from ..verification.minicheck import MiniCheck
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
        if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end < start:
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


class CandidateVerification(BaseModel):
    vendor_name: str
    verified: bool
    verifier_score: float
    claims: list[ClaimVerification] = Field(default_factory=list)
    unsupported_critical: list[str] = Field(default_factory=list)
    decision: str = "proceed"  # worst per-claim decision (proceed/regenerate/replan)
    grade: str = ""  # worst grade among verified claims


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
        score = round(min(r.verifier_score for r in results), 4)
        verified_grades = [r.grade for r in results if r.verified and r.grade]
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
            field=claim.field, subject=subject,
        )
        cited_supported = result.supported
        stage = "minicheck"
        if not result.supported:
            corpus = self._corpus(exclude=premise)
            reverified = self.safe.reverify(claim, corpus=corpus)
            if reverified.score > result.score:
                result = reverified
            stage = "minicheck+safe"
        grounding = classify_grounding(
            supported_on_cited=cited_supported,
            supported_on_corpus=(not cited_supported) and result.supported,
            subject=subject, value=claim.object_value, cited_span=premise,
        )
        graded = grade_claim(
            source_class=self._source_class_for(ref),
            exact_span=self._has_exact_span(ref),
            grounding=grounding.label,
            corroborating_spans=self._corroborations(claim, subject, premise)
            if grounding.label == "grounded" else 1,
        )
        verification = ClaimVerification(
            claim_id=claim.claim_id, field=claim.field, subject=claim.subject,
            predicate=claim.predicate, verified=result.supported,
            verifier_score=result.score, critical=claim.critical, stage=stage,
            rationale=result.rationale,
            grounding=grounding.label, decision=grounding.decision,
            grade=graded.grade,
        )
        self._write_back(ref, verification)
        return verification

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
                return max(classes, key=lambda c: DEFAULT_RELIABILITY.get(c, 0.0))
            return "unknown"
        # Extraction claim rows carry no page text; classify their source page.
        parent = self.ledger.get(item.metadata.get("parent_ledger_id") or "")
        if parent is not None:
            item = parent
        return classify_source(host_of(item.final_url or item.url),
                               text=item.text or "", title=item.title or "")

    def _has_exact_span(self, ref) -> bool:
        """True when the claim row carries verified character-span offsets."""
        item = self.ledger.get(getattr(ref, "ledger_id", "")) if ref is not None else None
        if item is None:
            return False
        meta = item.metadata
        return isinstance(meta.get("start_char"), int) and isinstance(meta.get("end_char"), int)

    def _corroborations(self, claim: AtomicClaim, subject: str, cited_premise: str) -> int:
        """1 (the cited span) + other ledger spans that also support the claim.

        Stops at CORROBORATION_UPGRADE: the count only feeds the GRADE +1
        threshold, so checking the remaining corpus (one entailment call per
        page) would be cost without signal.
        """
        from ..verification.grade import CORROBORATION_UPGRADE

        count = 1
        for span in self._corpus(exclude=cited_premise):
            if count >= CORROBORATION_UPGRADE:
                break
            check = self.minicheck.check(
                claim=claim.predicate, value=claim.object_value, evidence_span=span,
                field=claim.field, subject=subject,
            )
            if check.supported:
                count += 1
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

    def _corpus(self, *, exclude: str) -> list[str]:
        spans: list[str] = []
        for item in self.ledger.items():
            if not item.text:
                continue
            span = _join(item.title, item.text)
            if span and span != exclude:
                spans.append(span)
        return spans

    def _write_back(self, ref, verification: ClaimVerification) -> None:
        item = self.ledger.get(getattr(ref, "ledger_id", "")) if ref is not None else None
        if item is None or "claim_id" not in item.metadata:
            return  # only annotate genuine claim rows, not page-level evidence
        # annotate() owns the chain consequences: marks it stale for one lazy
        # reseal, and refuses if a tree_head commitment was already published.
        self.ledger.annotate(item.ledger_id, {
            "verified": verification.verified,
            "verifier_score": verification.verifier_score,
            "verifier_stage": verification.stage,
            "grounding": verification.grounding,
            "grade": verification.grade,
        })


def _join(title: str | None, text: str) -> str:
    return f"{title}\n{text}" if title else text
