"""SEA-first geography strategy: query templates, scoring, and global fallback.

SEA-first is a ranking boost, not a hard filter. If no SEA candidate validates
after the budgeted search/fetch attempts, the controller broadens to the global
templates.
"""

from __future__ import annotations

SEA_COUNTRIES = (
    "Singapore", "Malaysia", "Indonesia", "Thailand", "Vietnam",
    "Philippines", "Brunei", "Cambodia", "Laos", "Myanmar",
)
_COUNTRY_CODE = {
    "Singapore": "SG", "Malaysia": "MY", "Indonesia": "ID", "Thailand": "TH",
    "Vietnam": "VN", "Philippines": "PH", "Brunei": "BN", "Cambodia": "KH",
    "Laos": "LA", "Myanmar": "MM",
}

# Geo relevance scores (spec section 8).
EXACT_COUNTRY = 20.0
SEA_REGIONAL = 12.0
SERVES_REGION = 6.0
UNKNOWN = 0.0
CONFLICTING = -10.0

_LOCAL_PROCUREMENT_SUFFIXES = (
    "request quotation", "for quotation", "RFQ", "contact sales", "quotation",
)


def build_query_templates(
    service_or_product: str,
    region: str = "SEA",
    target_country: str | None = None,
    mode: str | None = None,
) -> list[str]:
    """Local procurement phrasing first (SEA), then global fallback templates."""
    base = service_or_product.strip()
    mode = mode or ""
    if region == "global":
        if mode == "product_exact_price":
            return [
                f"{base} price",
                f"{base} public pricing",
                f"{base} supplier price",
                f"{base} MOQ price",
            ]
        if mode == "contact_enrichment_only" or mode == "revalidation":
            return [
                f"{base} official contact",
                f"{base} email",
                f"{base} phone",
                f"{base} contact page",
            ]
        return [
            f"{base} vendor",
            f"{base} request quotation",
            f"{base} suppliers",
            f"{base} pricing",
        ]
    countries = [target_country] if target_country else ["Singapore", "Malaysia"]
    templates: list[str] = []
    for country in countries:
        if mode == "product_exact_price":
            suffixes = ("price", "public pricing", "bulk price", "MOQ price")
        elif mode == "contact_enrichment_only" or mode == "revalidation":
            suffixes = ("official contact", "email", "phone", "contact page")
        else:
            suffixes = _LOCAL_PROCUREMENT_SUFFIXES[:4]
        for suffix in suffixes:
            templates.append(f"{base} {country} {suffix}")
    if mode == "product_exact_price":
        templates.append(f"{base} Southeast Asia supplier price")
    elif mode == "contact_enrichment_only" or mode == "revalidation":
        templates.append(f"{base} Southeast Asia contact")
    else:
        templates.append(f"{base} Southeast Asia vendor")
    # de-dup preserving order
    seen: set[str] = set()
    out: list[str] = []
    for t in templates:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


class GeoStrategy:
    def __init__(self, boost_countries: list[str] | None = None, default_region: str = "SEA") -> None:
        self.boost_countries = boost_countries or list(SEA_COUNTRIES)
        self.default_region = default_region

    def location_code(self, country: str | None) -> str | None:
        if not country:
            return "SG" if self.default_region == "SEA" else None
        return _COUNTRY_CODE.get(country)

    def score(self, candidate_country: str | None, target_country: str | None, page_text: str = "") -> float:
        """Geo relevance score per spec; serves-region inferred from page text."""
        if candidate_country:
            if target_country and candidate_country == target_country:
                return EXACT_COUNTRY
            if candidate_country in self.boost_countries:
                return SEA_REGIONAL
        text = (page_text or "").lower()
        if any(c.lower() in text for c in self.boost_countries):
            return SERVES_REGION
        if candidate_country and target_country and candidate_country != target_country:
            # Known but contradicts an explicit target -> small penalty.
            return CONFLICTING if candidate_country not in self.boost_countries else SEA_REGIONAL
        return UNKNOWN
