"""Deterministic mode classifier.

Keyword/intent scoring picks a procurement mode without an LLM call so the
choice is reproducible and testable. An optional LLM hook can be injected later
without changing callers, but v1 ships the deterministic path as the default.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from .contracts import ProcurementMode

# Service categories where public pricing is rarely listed.
_SERVICE_TERMS = (
    "cleaning", "maintenance", "security guard", "guard service", "cctv",
    "installation", "pest control", "managed it", "it support", "fire alarm",
    "landscaping", "renovation", "catering", "facility management", "facilities",
    "repair", "servicing", "consultancy", "consulting", "audit", "training",
    "manpower", "courier", "logistics service", "disposal", "moving service",
)
_SERVICE_INTENT = ("quote", "quotation", "rfq", "request a quote", "contact sales", "tender")

# Product cues: countable goods and explicit pricing intent.
_PRODUCT_TERMS = (
    "chairs", "chair", "paper", "keyboard", "keyboards", "monitor", "monitors",
    "laptop", "laptops", "desk", "desks", "printer", "printers", "cartridge",
    "units", "supplier", "suppliers", "wholesale", "bulk", "moq", "pieces", "pcs",
)
_PRICE_INTENT = ("price", "pricing", "public pricing", "cost", "per unit", "unit price", "$")

_CONTACT_INTENT = (
    "contact", "contacts", "email", "phone number", "get in touch", "reach out",
    "enrich", "contact details", "find the email", "decision maker",
)
_REVALIDATION_INTENT = ("revalidate", "re-validate", "refresh", "verify again", "is it still", "still valid")

# T-R.4: electronics substitute vertical. Only fires on strong signals (substitute
# / obsolescence intent, or an MPN alongside a component term) so generic product
# and service queries keep their existing classification.
_ELECTRONICS_TERMS = (
    "ic", "mosfet", "mcu", "microcontroller", "capacitor", "resistor", "transistor",
    "connector", "op-amp", "opamp", "diode", "regulator", "datasheet", "semiconductor",
    "integrated circuit", "fpga", "eeprom", "voltage regulator",
)
_SUBSTITUTE_INTENT = (
    "substitute", "replacement", "cross reference", "cross-reference", "equivalent",
    "alternative part", "drop-in", "drop in", "pin compatible", "pin-compatible", "fff",
)
_OBSOLESCENCE_INTENT = (
    "obsolete", "eol", "end of life", "end-of-life", "nrnd", "nla", "ltb",
    "last time buy", "last-time-buy", "superseded", "discontinued", "pcn",
)
_MPN_RE = re.compile(r"\b[a-z]{1,6}-?\d{2,}[a-z0-9\-]*\b", re.IGNORECASE)

_QUANTITY_RE = re.compile(r"\b\d{2,}\b")


class ClassificationResult(BaseModel):
    mode: ProcurementMode
    confidence: float
    signals: dict[str, list[str]] = Field(default_factory=dict)
    rationale: str = ""


def _hits(text: str, terms: tuple[str, ...]) -> list[str]:
    return [t for t in terms if t in text]


class ModeClassifier:
    """Score each mode by matched intent terms; highest score wins."""

    def classify(self, query: str, forced_mode: str | None = None) -> ClassificationResult:
        if forced_mode and forced_mode != "auto":
            mode = ProcurementMode(forced_mode)
            return ClassificationResult(
                mode=mode, confidence=1.0, rationale="forced by caller"
            )

        text = (query or "").lower()
        service_terms = _hits(text, _SERVICE_TERMS)
        service_intent = _hits(text, _SERVICE_INTENT)
        product_terms = _hits(text, _PRODUCT_TERMS)
        price_intent = _hits(text, _PRICE_INTENT)
        contact_intent = _hits(text, _CONTACT_INTENT)
        reval_intent = _hits(text, _REVALIDATION_INTENT)
        has_quantity = bool(_QUANTITY_RE.search(text))

        elec_terms = _hits(text, _ELECTRONICS_TERMS)
        substitute_intent = _hits(text, _SUBSTITUTE_INTENT)
        obsolescence_intent = _hits(text, _OBSOLESCENCE_INTENT)
        has_mpn = bool(_MPN_RE.search(text))
        # Require a genuine substitute/obsolescence signal (or an MPN with a
        # component term) so plain product/service queries are never reclassified.
        elec_signal = bool(substitute_intent or obsolescence_intent or (has_mpn and elec_terms))
        electronics_score = (
            2.0 * len(substitute_intent) + 1.5 * len(obsolescence_intent)
            + 1.5 * len(elec_terms) + (1.5 if has_mpn else 0.0)
        ) if elec_signal else 0.0

        scores: dict[ProcurementMode, float] = {
            ProcurementMode.ELECTRONICS_SUBSTITUTION: electronics_score,
            ProcurementMode.REVALIDATION: 3.0 * len(reval_intent),
            ProcurementMode.CONTACT_ENRICHMENT_ONLY: (
                2.0 * len(contact_intent)
                - 1.5 * len(price_intent)
                - 1.0 * len(service_intent)
            ),
            ProcurementMode.SERVICE_QUOTE_REQUIRED: (
                2.0 * len(service_terms) + 1.5 * len(service_intent)
            ),
            ProcurementMode.PRODUCT_EXACT_PRICE: (
                1.5 * len(product_terms) + 1.5 * len(price_intent) + (1.0 if has_quantity else 0.0)
            ),
        }

        mode = max(scores, key=lambda m: scores[m])
        top = scores[mode]
        # Default to service quote mode for ambiguous procurement phrasing: it is
        # the safest (RFQ draft only, never auto-submits) and the most common B2B case.
        if top <= 0:
            mode = ProcurementMode.SERVICE_QUOTE_REQUIRED

        ordered = sorted(scores.values(), reverse=True)
        margin = ordered[0] - ordered[1] if len(ordered) > 1 else ordered[0]
        confidence = max(0.3, min(0.99, 0.5 + 0.15 * margin))

        return ClassificationResult(
            mode=mode,
            confidence=round(confidence, 2),
            signals={
                "service_terms": service_terms,
                "service_intent": service_intent,
                "product_terms": product_terms,
                "price_intent": price_intent,
                "contact_intent": contact_intent,
                "revalidation_intent": reval_intent,
                "electronics_terms": elec_terms,
                "substitute_intent": substitute_intent,
                "obsolescence_intent": obsolescence_intent,
            },
            rationale=f"selected {mode.value} (score={top:.1f}, margin={margin:.1f})",
        )
