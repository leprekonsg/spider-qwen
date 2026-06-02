"""T-2.4: shared source classification + reliability priors.

One place classifies a host into a source tier (manufacturer / distributor /
aggregator / broker / marketplace / government / business / unknown) and maps
that tier to a reliability prior. Both the page judge (T-2.1 gate) and the
evidence ledger (per-claim confidence + Merkle chain) consume this module, so the
domain lists and tiering live here once rather than being duplicated.

The priors here are the runtime defaults; ``governance/policy_config.yaml``
mirrors them as the documented, overridable policy (``Policy.source_reliability``).
"""

from __future__ import annotations

from urllib.parse import urlparse

# Named authoritative sources (suffix-matched so subdomains count, not substrings
# -- "multi.com" must not read as the manufacturer "ti.com").
MANUFACTURERS = (
    "ti.com", "analog.com", "st.com", "nxp.com", "infineon.com", "microchip.com",
    "onsemi.com", "renesas.com", "rohm.com", "vishay.com", "diodes.com",
    "hirose.com", "molex.com", "te.com", "amphenol.com", "samtec.com",
    "murata.com", "tdk.com", "bourns.com",
)
DISTRIBUTORS = (
    "digikey.com", "digikey.sg", "mouser.com", "mouser.sg", "arrow.com",
    "avnet.com", "farnell.com", "newark.com", "element14.com", "rs-online.com",
    "rsdelivers.com", "future.com", "verical.com",
)
AGGREGATORS = (
    "octopart.com", "oemsecrets.com", "findchips.com", "alldatasheet.com",
    "datasheets.com", "componentsearchengine.com",
)
# Brokers + marketplaces are keyword-matched: domains vary, and the consequence
# (low reliability) is conservative either way.
BROKERS = (
    "rochester", "lansdale", "brokerforum", "netcomponents", "icsource",
)
MARKETPLACES = (
    "alibaba", "aliexpress", "made-in-china", "ebay", "amazon", "indiamart",
    "tradeindia", "dhgate",
)

_PROCUREMENT_SIGNALS = (
    "quote", "quotation", "rfq", "vendor", "supplier", "price", "pricing",
    "contact", "sales", "catalog", "catalogue", "datasheet", "stock", "moq",
    "pte ltd", "we supply", "we provide", "request a quote",
)

# Reliability prior per tier (spec section: mfr datasheet 0.99, authorized dist
# 0.95, aggregator 0.85, broker 0.4-0.6, marketplace 0.1-0.3 w/ mandatory auth).
DEFAULT_RELIABILITY: dict[str, float] = {
    "manufacturer": 0.99,
    "distributor": 0.95,
    "government": 0.9,
    "aggregator": 0.85,
    "business": 0.6,
    "broker": 0.5,
    "unknown": 0.4,
    "marketplace": 0.2,
}


def host_of(url: str) -> str:
    host = (urlparse(url).netloc or url or "").lower()
    return host[4:] if host.startswith("www.") else host


def _suffix_match(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def classify_source(host: str, text: str = "", title: str = "") -> str:
    """Classify a host into a source tier. ``text``/``title`` only disambiguate
    the business-vs-unknown fallback for non-named domains."""
    if any(k in host for k in MARKETPLACES):
        return "marketplace"
    if _suffix_match(host, DISTRIBUTORS):
        return "distributor"
    if _suffix_match(host, MANUFACTURERS):
        return "manufacturer"
    if any(k in host for k in BROKERS):
        return "broker"
    if _suffix_match(host, AGGREGATORS):
        return "aggregator"
    if host.endswith(".gov") or ".gov." in host or host.endswith(".edu") or ".edu." in host:
        return "government"
    blob = f"{title} {text}".lower()
    return "business" if any(s in blob for s in _PROCUREMENT_SIGNALS) else "unknown"


def reliability_for(
    url: str, *, text: str = "", title: str = "", priors: dict[str, float] | None = None
) -> tuple[str, float]:
    """Return ``(source_class, reliability_prior)`` for a URL."""
    table = {**DEFAULT_RELIABILITY, **(priors or {})}
    source_class = classify_source(host_of(url), text, title)
    return source_class, float(table.get(source_class, DEFAULT_RELIABILITY["unknown"]))
