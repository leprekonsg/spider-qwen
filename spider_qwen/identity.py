"""Shared, deterministic supplier identity helpers."""

from __future__ import annotations

import hashlib
import ipaddress
import unicodedata
from urllib.parse import urlsplit

import tldextract


# The packaged snapshot is the only suffix source. This prevents a first-use
# network request and makes identity reproducible for a pinned tldextract build.
_PSL = tldextract.TLDExtract(
    suffix_list_urls=(),
    cache_dir=None,
    include_psl_private_domains=True,
)

_CORPORATE_SUFFIXES: tuple[tuple[str, ...], ...] = (
    ("sendirian", "berhad"),
    ("private", "limited"),
    ("proprietary", "limited"),
    ("pte", "ltd"),
    ("sdn", "bhd"),
    ("pvt", "ltd"),
    ("pty", "ltd"),
    ("incorporated",),
    ("corporation",),
    ("company",),
    ("limited",),
    ("berhad",),
    ("gmbh",),
    ("corp",),
    ("inc",),
    ("llp",),
    ("llc",),
    ("plc",),
    ("ltd",),
    ("co",),
)


def normalize_supplier_name(name: str) -> str:
    """Normalize a supplier name while preserving letters from every script."""
    normalized = unicodedata.normalize("NFKC", name or "").casefold()
    tokens = "".join(
        char
        if (
            char.isalnum()
            or char.isspace()
            or unicodedata.category(char).startswith("M")
        )
        else " "
        for char in normalized
    ).split()
    while tokens:
        matched = False
        for suffix in _CORPORATE_SUFFIXES:
            if len(tokens) >= len(suffix) and tuple(tokens[-len(suffix) :]) == suffix:
                del tokens[-len(suffix) :]
                matched = True
                break
        if not matched:
            break
    return " ".join(tokens)


def _hostname(url_or_host: str | None) -> str:
    raw = (url_or_host or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw if "://" in raw or raw.startswith("//") else f"//{raw}")
        host = (parsed.hostname or "").strip(".").casefold()
    except ValueError:
        return ""
    if not host:
        return ""
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return ""


def registrable_domain(url_or_host: str | None) -> str:
    """Return the PSL-aware registrable domain without making network calls."""
    host = _hostname(url_or_host)
    if not host:
        return ""
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    result = _PSL(host)
    return result.top_domain_under_public_suffix or host


def stable_supplier_id(
    vendor_name: str,
    website: str | None = None,
    country: str | None = None,
    *,
    legal_name: str | None = None,
    branch: str | None = None,
    marketplace_storefront: str | None = None,
) -> str:
    """Build a stable opaque ID from the strongest available identity evidence."""
    name = normalize_supplier_name(legal_name or vendor_name)
    domain = registrable_domain(website)
    storefront = _canonical_storefront(marketplace_storefront)
    normalized_branch = normalize_supplier_name(branch or "")
    if storefront:
        seed = f"storefront={storefront}|name={name}|branch={normalized_branch}"
    elif domain:
        seed = f"domain={domain}|name={name}|branch={normalized_branch}"
    else:
        normalized_country = unicodedata.normalize("NFKC", country or "").casefold().strip()
        seed = f"name={name}|country={normalized_country}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]
    return f"sup_{digest}"


def ensure_supplier_id(candidate: object) -> str:
    """Populate and return a candidate's supplier_id when it is absent."""
    current = str(getattr(candidate, "supplier_id", "") or "").strip()
    if current:
        return current
    supplier_id = stable_supplier_id(
        str(getattr(candidate, "vendor_name", "") or ""),
        getattr(candidate, "website", None),
        getattr(candidate, "country", None),
        legal_name=getattr(candidate, "legal_name", None),
        branch=getattr(candidate, "branch", None),
        marketplace_storefront=getattr(candidate, "marketplace_storefront", None),
    )
    setattr(candidate, "supplier_id", supplier_id)
    return supplier_id


def candidates_same_supplier(left: object, right: object) -> bool:
    """Return true only when available identity evidence is compatible.

    A shared host is insufficient when names differ, and a shared name is
    insufficient when the candidates identify different domains or countries.
    Explicit matching supplier IDs are treated as an upstream identity assertion.
    """
    left_name = normalize_supplier_name(str(
        getattr(left, "legal_name", None) or getattr(left, "vendor_name", "") or ""
    ))
    right_name = normalize_supplier_name(str(
        getattr(right, "legal_name", None) or getattr(right, "vendor_name", "") or ""
    ))
    left_id = str(getattr(left, "supplier_id", "") or "").strip()
    right_id = str(getattr(right, "supplier_id", "") or "").strip()
    if not left_name or not right_name:
        left_raw = unicodedata.normalize(
            "NFKC", str(getattr(left, "vendor_name", "") or "")
        ).casefold().strip()
        right_raw = unicodedata.normalize(
            "NFKC", str(getattr(right, "vendor_name", "") or "")
        ).casefold().strip()
        return bool(left_raw and left_raw == right_raw and left_id == right_id)
    if left_id and right_id and left_id == right_id:
        return True
    if not left_name or left_name != right_name:
        return False

    left_branch = normalize_supplier_name(str(getattr(left, "branch", "") or ""))
    right_branch = normalize_supplier_name(str(getattr(right, "branch", "") or ""))
    if left_branch and right_branch and left_branch != right_branch:
        return False
    left_storefront = _canonical_storefront(getattr(left, "marketplace_storefront", None))
    right_storefront = _canonical_storefront(getattr(right, "marketplace_storefront", None))
    if left_storefront and right_storefront and left_storefront != right_storefront:
        return False

    left_domain = registrable_domain(getattr(left, "website", None))
    right_domain = registrable_domain(getattr(right, "website", None))
    if left_domain or right_domain:
        return bool(left_domain and right_domain and left_domain == right_domain)

    left_country = unicodedata.normalize(
        "NFKC", str(getattr(left, "country", "") or "")
    ).casefold().strip()
    right_country = unicodedata.normalize(
        "NFKC", str(getattr(right, "country", "") or "")
    ).casefold().strip()
    return bool(left_country and left_country == right_country)


def _canonical_storefront(url: str | None) -> str:
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw if "://" in raw else f"https://{raw}")
    except ValueError:
        return ""
    host = _hostname(raw)
    if not host:
        return ""
    path = "/".join(segment for segment in parsed.path.casefold().split("/") if segment)
    return f"{host}/{path}".rstrip("/")


# Backward-compatible spelling used by extraction callers.
normalize_vendor_name = normalize_supplier_name


__all__ = [
    "candidates_same_supplier",
    "ensure_supplier_id",
    "normalize_supplier_name",
    "normalize_vendor_name",
    "registrable_domain",
    "stable_supplier_id",
]
