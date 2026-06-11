"""Pricing extraction and the PricingStatus ontology.

Examples (from the spec):
  "$10 per unit"               -> EXACT_PRICE
  "$10-$20 per unit"           -> PRICE_RANGE
  "From $99/month"             -> STARTING_FROM
  "Download our rate card"     -> RATE_CARD_FOUND
  "Request a quotation"        -> QUOTE_REQUIRED
  "Contact sales for pricing"  -> CONTACT_FOR_PRICING
  no pricing/quote evidence    -> NOT_FOUND
  two incompatible prices      -> CONFLICTING
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from pydantic import BaseModel

from ..modes.contracts import PricingStatus

# Currency tokens -> ISO-ish code. A bare "$" is ambiguous and resolved per
# page by _bare_dollar_currency (same-page S$/SGD mention, then ccTLD).
_CURRENCY = {
    "s$": "SGD", "sgd": "SGD", "rm": "MYR", "myr": "MYR", "us$": "USD",
    "usd": "USD", "$": "USD", "€": "EUR", "eur": "EUR", "£": "GBP", "gbp": "GBP",
    "rp": "IDR", "idr": "IDR", "฿": "THB", "thb": "THB", "₫": "VND", "vnd": "VND",
    "₱": "PHP", "php": "PHP",
}

_CURRENCY_TOKEN = r"(?:S\$|US\$|RM|Rp|SGD|MYR|USD|EUR|GBP|IDR|THB|VND|PHP|[$€£฿₫₱])"
_NUMBER = r"\d[\d,]*(?:\.\d+)?"

_PRICE_RE = re.compile(rf"({_CURRENCY_TOKEN})\s?({_NUMBER})", re.IGNORECASE)
_RANGE_RE = re.compile(
    rf"{_CURRENCY_TOKEN}\s?{_NUMBER}\s?(?:-|–|to)\s?(?:{_CURRENCY_TOKEN}\s?)?{_NUMBER}",
    re.IGNORECASE,
)
_UNIT_RE = re.compile(
    r"(?:per|/)\s?(unit|month|year|piece|pcs|pax|person|hour|hr|sqft|sq ft|sqm|day|seat|user|item|kg|ton)",
    re.IGNORECASE,
)
_STARTING_RE = re.compile(r"\b(from|starting (?:from|at)|as low as|starts? at)\b", re.IGNORECASE)
# No trailing \b on "pric" branches: "pricing" continues with word chars, so a
# word boundary would never match right after "pric".
_RATE_CARD_RE = re.compile(r"\b(rate card|price list|pricing (?:sheet|pdf|guide)|download[^.]*pric)", re.IGNORECASE)
_QUOTE_RE = re.compile(
    r"\b(request (?:a )?(?:quote|quotation)|get (?:a )?quote|ask for (?:a )?quote|rfq|enquire (?:now|for pricing)|request pricing)\b",
    re.IGNORECASE,
)
_CONTACT_PRICING_RE = re.compile(
    r"\b(contact (?:us |our )?(?:sales |team )?for pric|call for pric|contact for pric|price on request|p\.?o\.?a)",
    re.IGNORECASE,
)


class PricingResult(BaseModel):
    status: PricingStatus
    price: float | None = None
    currency: str | None = None
    unit: str | None = None
    matched_text: str = ""


def _normalize_currency(token: str) -> str | None:
    return _CURRENCY.get(token.strip().lower())


# Bare "$" on a Singapore page means SGD: the page either says S$/SGD
# somewhere, or sits on a .sg host (covers .com.sg etc. via endswith).
_SGD_HINT_RE = re.compile(r"S\$|\bSGD\b", re.IGNORECASE)


def _bare_dollar_currency(text: str, page_url: str | None) -> str:
    if _SGD_HINT_RE.search(text):
        return "SGD"
    host = urlparse(page_url).netloc.lower() if page_url else ""
    if host.endswith(".sg"):
        return "SGD"
    return "USD"


def _to_float(num: str) -> float | None:
    try:
        return float(num.replace(",", ""))
    except ValueError:
        return None


class PricingExtractor:
    """Classify pricing evidence on a page into a PricingStatus."""

    def extract(self, text: str, page_url: str | None = None) -> PricingResult:
        text = text or ""
        bare_dollar: str | None = None
        prices = []
        for m in _PRICE_RE.finditer(text):
            token = m.group(1)
            if token.strip() == "$":
                if bare_dollar is None:
                    bare_dollar = _bare_dollar_currency(text, page_url)
                currency = bare_dollar
            else:
                currency = _normalize_currency(token)
            prices.append((currency, _to_float(m.group(2)), m.group(0)))
        prices = [(c, v, raw) for c, v, raw in prices if v is not None]
        has_range = bool(_RANGE_RE.search(text))
        has_starting = bool(_STARTING_RE.search(text))
        unit_match = _UNIT_RE.search(text)
        unit = unit_match.group(1).lower() if unit_match else None

        if prices:
            first_cur, first_val, first_raw = prices[0]
            if has_range:
                return PricingResult(
                    status=PricingStatus.PRICE_RANGE, price=first_val,
                    currency=first_cur, unit=unit, matched_text=_RANGE_RE.search(text).group(0),
                )
            if has_starting:
                return PricingResult(
                    status=PricingStatus.STARTING_FROM, price=first_val,
                    currency=first_cur, unit=unit, matched_text=first_raw,
                )
            distinct = {v for _, v, _ in prices}
            if len(distinct) >= 2:
                lo, hi = min(distinct), max(distinct)
                if hi > lo * 1.5:  # incompatible claims for the same thing
                    return PricingResult(
                        status=PricingStatus.CONFLICTING, currency=first_cur,
                        unit=unit, matched_text="; ".join(raw for _, _, raw in prices[:3]),
                    )
            return PricingResult(
                status=PricingStatus.EXACT_PRICE, price=first_val,
                currency=first_cur, unit=unit, matched_text=first_raw,
            )

        if _RATE_CARD_RE.search(text):
            return PricingResult(status=PricingStatus.RATE_CARD_FOUND, matched_text=_RATE_CARD_RE.search(text).group(0))
        if _QUOTE_RE.search(text):
            return PricingResult(status=PricingStatus.QUOTE_REQUIRED, matched_text=_QUOTE_RE.search(text).group(0))
        if _CONTACT_PRICING_RE.search(text):
            return PricingResult(status=PricingStatus.CONTACT_FOR_PRICING, matched_text=_CONTACT_PRICING_RE.search(text).group(0))
        return PricingResult(status=PricingStatus.NOT_FOUND)
