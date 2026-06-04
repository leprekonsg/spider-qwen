"""FActScore-style atomic decomposition of a candidate into checkable claims.

Each validated candidate asserts a small set of atomic facts (vendor name, price,
MOQ, quote channel, contact). Decomposition turns those into ``AtomicClaim``
records the verification spine can check one (claim, evidence_span) pair at a
time. It is pure and duck-typed (reads attributes via ``getattr``) so it never
imports the candidate models and stays trivially unit-testable.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ..evidence.models import sha256_hex

# Pricing statuses that do NOT assert a concrete price. A populated price with any
# other status (including unknown/None) is treated as a critical claim: if it
# cannot be grounded the candidate is not trustworthy procurement evidence. This
# is conservative on purpose -- a fabricated price must never slip through because
# its status field happened to be missing or unrecognized.
_NON_PRICED = {"NOT_FOUND", "QUOTE_REQUIRED", "CONTACT_FOR_PRICING", "CONFLICTING"}


class AtomicClaim(BaseModel):
    claim_id: str
    field: str
    subject: str
    predicate: str  # human-readable assertion, used as the entailment hypothesis
    object_value: str = ""  # the concrete value to ground (price/email/phone/...)
    critical: bool = False  # a failed critical claim blocks the whole candidate
    evidence_ref: Any = None  # optional, most-specific EvidenceRef on a sub-object


def _claim_id(subject: str, field: str, value: str) -> str:
    return "vclaim_" + sha256_hex(f"{subject}|{field}|{value}")[:12]


def _status_str(status: Any) -> str:
    return str(getattr(status, "value", status) or "")


def _fmt_number(n: Any) -> str:
    try:
        f = float(n)
    except (TypeError, ValueError):
        return str(n)
    return str(int(f)) if f.is_integer() else str(f)


def decompose(candidate: Any) -> list[AtomicClaim]:
    """Break a candidate into atomic, evidence-checkable claims."""
    subject = (getattr(candidate, "vendor_name", "") or "").strip()
    claims: list[AtomicClaim] = []

    if subject and subject.lower() != "unknown vendor":
        claims.append(AtomicClaim(
            claim_id=_claim_id(subject, "vendor_name", subject), field="vendor_name",
            subject=subject, predicate=f"The vendor is {subject}.",
            object_value=subject, critical=False,
        ))

    price = getattr(candidate, "price", None)
    if price is not None:
        currency = getattr(candidate, "currency", None) or ""
        unit = getattr(candidate, "unit", None) or ""
        value = _fmt_number(price)
        status = _status_str(getattr(candidate, "pricing_status", None))
        predicate = f"{subject} lists a price of {currency} {value} {unit}".strip()
        claims.append(AtomicClaim(
            claim_id=_claim_id(subject, "price", value), field="price",
            subject=subject, predicate=predicate, object_value=value,
            critical=status not in _NON_PRICED,
        ))

    moq = getattr(candidate, "moq", None)
    if moq:
        claims.append(AtomicClaim(
            claim_id=_claim_id(subject, "moq", str(moq)), field="moq",
            subject=subject, predicate=f"{subject} has a minimum order quantity of {moq}.",
            object_value=str(moq), critical=False,
        ))

    quote_channel = getattr(candidate, "quote_channel", None)
    if quote_channel is not None:
        value = getattr(quote_channel, "value", "") or ""
        claims.append(AtomicClaim(
            claim_id=_claim_id(subject, "quote_channel", value), field="quote_channel",
            subject=subject, predicate=f"{subject} accepts quote requests via {value}.",
            object_value=value, critical=True,
            evidence_ref=getattr(quote_channel, "evidence_ref", None),
        ))

    for contact in getattr(candidate, "contacts", None) or []:
        value = getattr(contact, "value", "") or ""
        ctype = getattr(contact, "type", "contact")
        claims.append(AtomicClaim(
            claim_id=_claim_id(subject, f"contact_{ctype}", value), field=f"contact_{ctype}",
            subject=subject, predicate=f"{subject} can be reached at {value} ({ctype}).",
            object_value=value, critical=True,
            evidence_ref=getattr(contact, "evidence_ref", None),
        ))

    return claims
