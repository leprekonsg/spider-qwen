"""Deterministic offering identity layered below supplier identity."""

from __future__ import annotations

import hashlib
import unicodedata

from .identity import ensure_supplier_id


def normalize_offering_name(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return " ".join(
        "".join(
            char if char.isalnum() or char.isspace() else " "
            for char in normalized
        ).split()
    )


def stable_offering_id(
    supplier_id: str,
    item: str | None,
    *,
    kind: str,
    variant: str | None = None,
) -> str:
    """Return an opaque ID, or empty when the offering is not identified."""
    normalized_item = normalize_offering_name(item)
    if not supplier_id or not normalized_item:
        return ""
    seed = (
        f"supplier={supplier_id}|kind={normalize_offering_name(kind)}|"
        f"item={normalized_item}|variant={normalize_offering_name(variant)}"
    )
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:20]
    return f"ofr_{digest}"


def ensure_offering_id(candidate: object) -> str:
    current = str(getattr(candidate, "offering_id", "") or "").strip()
    if current:
        return current
    supplier_id = ensure_supplier_id(candidate)
    if hasattr(candidate, "product_name"):
        item = getattr(candidate, "product_name", "")
        variant = getattr(candidate, "variant", None)
        kind = "product"
    elif hasattr(candidate, "service_name"):
        item = getattr(candidate, "service_name", "")
        variant = getattr(candidate, "service_variant", None)
        kind = "service"
    else:
        return ""
    offering_id = stable_offering_id(supplier_id, item, kind=kind, variant=variant)
    if offering_id:
        setattr(candidate, "offering_id", offering_id)
    return offering_id


def candidates_same_offering(left: object, right: object) -> bool:
    """Decide whether two same-supplier observations may be consolidated.

    Product observations require a known, equal offering. Service observations
    retain the existing complementary-page merge unless both sides explicitly
    identify different services.
    """
    left_id = ensure_offering_id(left)
    right_id = ensure_offering_id(right)
    left_product = hasattr(left, "product_name")
    right_product = hasattr(right, "product_name")
    if left_product or right_product:
        return left_product and right_product and bool(left_id and left_id == right_id)
    if left_id and right_id:
        return left_id == right_id
    return True


def candidate_selector_key(candidate: object) -> tuple[str, str]:
    """Stable row key for maps that may contain multiple supplier offerings."""
    if isinstance(candidate, dict):
        return (
            str(candidate.get("supplier_id", "") or ""),
            str(candidate.get("offering_id", "") or ""),
        )
    return (
        str(getattr(candidate, "supplier_id", "") or ""),
        str(getattr(candidate, "offering_id", "") or ""),
    )


__all__ = [
    "candidate_selector_key",
    "candidates_same_offering",
    "ensure_offering_id",
    "normalize_offering_name",
    "stable_offering_id",
]
