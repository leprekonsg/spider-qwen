"""Span-level evidence verification."""

from __future__ import annotations

from pydantic import BaseModel, Field

from .ledger import EvidenceLedger
from .models import sha256_hex


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
