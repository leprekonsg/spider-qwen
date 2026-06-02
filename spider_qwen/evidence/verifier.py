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


class CandidateVerification(BaseModel):
    vendor_name: str
    verified: bool
    verifier_score: float
    claims: list[ClaimVerification] = Field(default_factory=list)
    unsupported_critical: list[str] = Field(default_factory=list)


class VerificationSpine:
    """Decompose -> MiniCheck -> SAFE, writing verdicts back onto claim rows.

    A claim is grounded against its source *page* text (``parent_ledger_id``),
    never the extraction snippet, so a fabricated value whose snippet is
    self-referential is still caught. A candidate is ``verified`` only when every
    critical claim holds; the controller blocks the rest from output.
    """

    def __init__(self, ledger: EvidenceLedger, *, minicheck: MiniCheck | None = None,
                 safe: SafeReverifier | None = None, threshold: float = 0.5) -> None:
        self.ledger = ledger
        self.minicheck = minicheck or MiniCheck(threshold=threshold)
        self.safe = safe or SafeReverifier(self.minicheck)

    def verify_candidate(self, candidate: Any) -> CandidateVerification:
        results = [self._verify_claim(candidate, claim) for claim in decompose(candidate)]
        unsupported_critical = [r.claim_id for r in results if r.critical and not r.verified]
        score = round(min((r.verifier_score for r in results), default=1.0), 4)
        return CandidateVerification(
            vendor_name=getattr(candidate, "vendor_name", "") or "",
            verified=not unsupported_critical, verifier_score=score,
            claims=results, unsupported_critical=unsupported_critical,
        )

    def _verify_claim(self, candidate: Any, claim: AtomicClaim) -> ClaimVerification:
        ref = self._ref_for_claim(candidate, claim)
        premise = self._premise_from_ref(ref)
        result = self.minicheck.check(claim=claim.predicate, value=claim.object_value,
                                      evidence_span=premise, field=claim.field)
        stage = "minicheck"
        if not result.supported:
            corpus = self._corpus(exclude=premise)
            reverified = self.safe.reverify(claim, corpus=corpus)
            if reverified.score > result.score:
                result = reverified
            stage = "minicheck+safe"
        verification = ClaimVerification(
            claim_id=claim.claim_id, field=claim.field, subject=claim.subject,
            predicate=claim.predicate, verified=result.supported,
            verifier_score=result.score, critical=claim.critical, stage=stage,
            rationale=result.rationale,
        )
        self._write_back(ref, verification)
        return verification

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
        return [item.text for item in self.ledger.items() if item.text and item.text != exclude]

    def _write_back(self, ref, verification: ClaimVerification) -> None:
        item = self.ledger.get(getattr(ref, "ledger_id", "")) if ref is not None else None
        if item is None or "claim_id" not in item.metadata:
            return  # only annotate genuine claim rows, not page-level evidence
        item.metadata["verified"] = verification.verified
        item.metadata["verifier_score"] = verification.verifier_score
        item.metadata["verifier_stage"] = verification.stage


def _join(title: str | None, text: str) -> str:
    return f"{title}\n{text}" if title else text
