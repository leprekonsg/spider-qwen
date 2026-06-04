"""Promotion + conflict rules for semantic memory (spec section 5.3, T-2.3).

- ``should_promote_contact``: promote a contact when 2+ independent evidence
  sources match, or 1 high-confidence source plus a domain-ownership signal.
- ``contradicts``: the cross-source contradiction predicate used by
  ``SemanticMemory.upsert`` to decide when two values for the same
  (entity, property) genuinely conflict (and must both be retained as a disputed
  record) versus merely differing in case/spacing/punctuation.
"""

from __future__ import annotations

from ..evidence.models import EvidenceRef

HIGH_CONFIDENCE = 0.8


def contradicts(value_a: str, value_b: str) -> bool:
    """True when two values for one (entity, property) genuinely conflict.

    Deterministic: compares case/whitespace/punctuation-normalized values, so
    "active" and "Active " agree while "active" and "NRND" contradict.
    """
    return _norm_value(value_a) != _norm_value(value_b)


def _norm_value(value: str) -> str:
    return "".join(c for c in (value or "").lower() if c.isalnum())


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
