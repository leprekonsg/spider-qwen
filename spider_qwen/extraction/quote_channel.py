"""Quote channel extraction.

Finds how a buyer can request a quote: RFQ form, contact email, phone, contact
page, rate card, or a login-walled portal. Service mode requires at least one
evidenced quote channel before an RFQ draft is produced.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from pydantic import BaseModel

from ..modes.contracts import QuoteChannelType

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?:\+?\d{1,3}[\s\-]?)?(?:\(?\d{2,4}\)?[\s\-]?){2,4}\d{2,4}")
_RFQ_LINK_RE = re.compile(r"(rfq|request[-_]?(?:a[-_]?)?quot|get[-_]?(?:a[-_]?)?quot|quotation|enquir)", re.IGNORECASE)
_CONTACT_LINK_RE = re.compile(r"(contact|reach[-_]?us|get[-_]?in[-_]?touch|enquir)", re.IGNORECASE)
_RATE_CARD_RE = re.compile(r"(rate[-_ ]?card|price[-_ ]?list)", re.IGNORECASE)
_PORTAL_LOGIN_RE = re.compile(
    r"(login to view|member login|supplier portal|sign in to (?:view|request)|portal login|register to view pric)",
    re.IGNORECASE,
)

# Quality ordering used when choosing the single best channel for an RFQ draft.
_PRIORITY = {
    QuoteChannelType.RFQ_FORM: 6,
    QuoteChannelType.CONTACT_EMAIL: 5,
    QuoteChannelType.CONTACT_PAGE: 4,
    QuoteChannelType.PHONE: 3,
    QuoteChannelType.RATE_CARD: 2,
    QuoteChannelType.PORTAL_LOGIN_REQUIRED: 1,
}


class QuoteChannelMatch(BaseModel):
    type: QuoteChannelType
    value: str
    matched_text: str = ""

    @property
    def quality(self) -> int:
        return _PRIORITY[self.type]


class QuoteChannelExtractor:
    def extract(self, text: str, links: list[str] | None = None, page_url: str = "") -> list[QuoteChannelMatch]:
        text = text or ""
        links = links or []
        matches: list[QuoteChannelMatch] = []
        seen: set[tuple[str, str]] = set()

        def add(ctype: QuoteChannelType, value: str, matched: str) -> None:
            key = (ctype.value, value)
            if value and key not in seen:
                seen.add(key)
                matches.append(QuoteChannelMatch(type=ctype, value=value, matched_text=matched))

        for link in links:
            if _RFQ_LINK_RE.search(link):
                add(QuoteChannelType.RFQ_FORM, link, "rfq link")
            elif _CONTACT_LINK_RE.search(link):
                add(QuoteChannelType.CONTACT_PAGE, link, "contact link")
            elif _RATE_CARD_RE.search(link):
                add(QuoteChannelType.RATE_CARD, link, "rate card link")

        email = _EMAIL_RE.search(text)
        if email:
            add(QuoteChannelType.CONTACT_EMAIL, email.group(0), email.group(0))

        phone = _PHONE_RE.search(text)
        if phone and len(re.sub(r"\D", "", phone.group(0))) >= 7:
            add(QuoteChannelType.PHONE, phone.group(0).strip(), phone.group(0).strip())

        if _PORTAL_LOGIN_RE.search(text):
            add(QuoteChannelType.PORTAL_LOGIN_REQUIRED, page_url or "portal", _PORTAL_LOGIN_RE.search(text).group(0))

        if _RATE_CARD_RE.search(text):
            add(QuoteChannelType.RATE_CARD, page_url or "rate_card", _RATE_CARD_RE.search(text).group(0))

        return matches

    def best(self, matches: list[QuoteChannelMatch]) -> QuoteChannelMatch | None:
        return max(matches, key=lambda m: m.quality) if matches else None
