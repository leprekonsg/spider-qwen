"""Vendor metadata: vendor_name, website, country from a fetched page."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from pydantic import BaseModel

# Country signals: corporate suffix, ccTLD, and explicit country mention.
_SUFFIX_COUNTRY = {
    "pte ltd": "Singapore", "pte. ltd": "Singapore", "sdn bhd": "Malaysia",
    "co., ltd": None, "pt ": "Indonesia", "ltd": None, "inc": None,
}
_TLD_COUNTRY = {
    ".sg": "Singapore", ".my": "Malaysia", ".id": "Indonesia", ".th": "Thailand",
    ".vn": "Vietnam", ".ph": "Philippines", ".bn": "Brunei", ".kh": "Cambodia",
    ".la": "Laos", ".mm": "Myanmar",
}
_COUNTRY_MENTIONS = (
    "Singapore", "Malaysia", "Indonesia", "Thailand", "Vietnam", "Philippines",
    "Brunei", "Cambodia", "Laos", "Myanmar",
)
_TITLE_NOISE = re.compile(r"\s*[|\-–—:]\s*(home|contact(?: us)?|about(?: us)?|welcome|official site).*$", re.IGNORECASE)


class VendorMetadata(BaseModel):
    vendor_name: str
    website: str | None = None
    country: str | None = None


class VendorMetadataExtractor:
    def extract(self, *, page_url: str, final_url: str | None, title: str | None, text: str) -> VendorMetadata:
        url = final_url or page_url or ""
        host = urlparse(url).netloc.lower()
        registrable = host[4:] if host.startswith("www.") else host
        website = f"https://{registrable}" if registrable else None

        name = self._vendor_name(title, text, registrable)
        country = self._country(text, host)
        return VendorMetadata(vendor_name=name, website=website, country=country)

    def _vendor_name(self, title: str | None, text: str, registrable: str) -> str:
        if title:
            cleaned = _TITLE_NOISE.sub("", title).strip()
            if cleaned:
                return cleaned
        # Fall back to the second-level domain, title-cased.
        if registrable:
            label = registrable.split(".")[0]
            return label.replace("-", " ").title()
        return "Unknown Vendor"

    def _country(self, text: str, host: str) -> str | None:
        lower = (text or "").lower()
        for suffix, country in _SUFFIX_COUNTRY.items():
            if country and suffix in lower:
                return country
        for tld, country in _TLD_COUNTRY.items():
            if host.endswith(tld):
                return country
        for mention in _COUNTRY_MENTIONS:
            if mention.lower() in lower:
                return mention
        return None
