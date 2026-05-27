"""Contact extraction: emails, phones, contact forms/pages with privacy class.

Role-based addresses (sales@, info@) are business_contact; addresses that look
like a named person (jane.doe@) are tagged named_person_high_sensitivity so the
governance layer can gate them.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from pydantic import BaseModel

from ..modes.contracts import PrivacyClass

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?:\+?\d{1,3}[\s\-]?)?(?:\(?\d{2,4}\)?[\s\-]?){2,4}\d{2,4}")
_CONTACT_LINK_RE = re.compile(r"(contact|reach[-_]?us|get[-_]?in[-_]?touch|enquir|about)", re.IGNORECASE)
_ROLE_LOCALPARTS = {
    "sales", "info", "contact", "enquiry", "enquiries", "inquiry", "support",
    "admin", "hello", "hi", "team", "general", "marketing", "service", "ask",
}
_NAMED_PERSON_RE = re.compile(r"^[a-z]+\.[a-z]+$")  # firstname.lastname


class ContactMatch(BaseModel):
    type: str  # email | phone | contact_form | contact_page
    value: str
    confidence: float = 0.0
    privacy_class: PrivacyClass = PrivacyClass.BUSINESS_CONTACT


def _classify_email(email: str) -> tuple[float, PrivacyClass]:
    local = email.split("@", 1)[0].lower()
    if local in _ROLE_LOCALPARTS:
        return 0.85, PrivacyClass.BUSINESS_CONTACT
    if _NAMED_PERSON_RE.match(local):
        return 0.7, PrivacyClass.NAMED_PERSON_HIGH_SENSITIVITY
    return 0.55, PrivacyClass.BUSINESS_CONTACT


class ContactExtractor:
    def extract(self, text: str, links: list[str] | None = None) -> list[ContactMatch]:
        text = text or ""
        links = links or []
        out: list[ContactMatch] = []
        seen: set[str] = set()

        for m in _EMAIL_RE.finditer(text):
            email = m.group(0).lower()
            if email in seen:
                continue
            seen.add(email)
            conf, privacy = _classify_email(email)
            out.append(ContactMatch(type="email", value=email, confidence=conf, privacy_class=privacy))

        for m in _PHONE_RE.finditer(text):
            raw = m.group(0).strip()
            digits = re.sub(r"\D", "", raw)
            if len(digits) < 7 or digits in seen:
                continue
            seen.add(digits)
            out.append(ContactMatch(type="phone", value=raw, confidence=0.5))

        for link in links:
            if _CONTACT_LINK_RE.search(link) and link not in seen:
                seen.add(link)
                kind = "contact_form" if "form" in link.lower() else "contact_page"
                out.append(ContactMatch(type=kind, value=link, confidence=0.4))

        return out
