"""Contact -> semantic memory promotion rule (spec section 5.3).

Promote a contact when:
- 2+ independent evidence sources match, OR
- 1 high-confidence source + a domain-ownership signal.
"""

from __future__ import annotations

from ..evidence.models import EvidenceRef

HIGH_CONFIDENCE = 0.8


def should_promote_contact(
    *,
    evidence_refs: list[EvidenceRef],
    confidence: float,
    domain_match: bool,
) -> bool:
    independent_sources = len({r.ledger_id for r in evidence_refs})
    if independent_sources >= 2:
        return True
    if independent_sources >= 1 and confidence >= HIGH_CONFIDENCE and domain_match:
        return True
    return False
