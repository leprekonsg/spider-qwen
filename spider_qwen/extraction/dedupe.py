"""Conservative supplier identity resolution and claim-level consolidation."""

from __future__ import annotations

import json
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel

from ..evidence.models import EvidenceRef
from ..identity import candidates_same_supplier, ensure_supplier_id, normalize_vendor_name, registrable_domain
from ..modes.contracts import CandidateFieldClaim
from ..offering_identity import candidates_same_offering, ensure_offering_id

_IDENTITY_FIELDS = {
    "schema_version", "supplier_id", "offering_id", "vendor_name", "website",
    "evidence_refs", "field_claims", "conflicting_fields",
}
_DERIVED_MAX_FIELDS = {"geo_score", "service_match_score", "service_match_evidence"}
_DERIVED_RESET_FIELDS = {
    "evidence_completeness", "checklist_completeness", "conflict_penalty",
    "score", "score_components", "requirement_assessments", "qualification",
}
_MULTIVALUED_FIELDS = {"quote_channel", "country", "product_url", "trading_name"}
_PRODUCT_OFFER_FIELDS = {
    "product_name", "variant", "quantity", "price", "currency", "unit", "moq",
    "geography", "valid_from", "valid_until", "offer_scope", "offer_scope_status",
    "pricing_status",
}
_SCOPE_DIMENSIONS = {
    "item", "variant", "quantity", "minimum_order_quantity", "currency", "unit",
    "geography", "valid_from", "valid_until",
}


def _registrable(website: str | None) -> str:
    """Compatibility shim for callers that used the old private helper."""
    return registrable_domain(website)


def _merge_refs(left: list[EvidenceRef], right: list[EvidenceRef]) -> list[EvidenceRef]:
    seen = {ref.ledger_id for ref in left}
    return left + [ref for ref in right if ref.ledger_id not in seen]


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def _claim_key(value: Any) -> str:
    return json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True, default=str)


def _value_evidence(
    candidate: object,
    field: str,
    value: Any,
) -> tuple[list[EvidenceRef], Literal["field", "candidate"]]:
    existing = (getattr(candidate, "field_claims", {}) or {}).get(field, [])
    for claim in existing:
        if _claim_key(claim.value) == _claim_key(value):
            return list(claim.evidence_refs), claim.evidence_scope
    if field == "quote_channel" and value is not None:
        ref = getattr(value, "evidence_ref", None)
        return ([ref] if ref is not None else []), "field"
    if field == "contacts" and isinstance(value, list):
        refs = _merge_refs([], [item.evidence_ref for item in value if hasattr(item, "evidence_ref")])
        return refs, "field"
    # Legacy candidates only carried candidate-level evidence. Preserve that
    # coarse relationship explicitly rather than claiming an exact field span.
    return list(getattr(candidate, "evidence_refs", []) or []), "candidate"


def _is_present(value: Any) -> bool:
    if isinstance(value, Enum) and value.value == "NOT_FOUND":
        return False
    return value is not None and value != "" and value != [] and value != {} and value is not False


def _record_claim(
    candidate: object,
    field: str,
    value: Any,
    refs: list[EvidenceRef],
    *,
    selected: bool,
    evidence_scope: Literal["field", "candidate"] = "field",
) -> None:
    claims = candidate.field_claims.setdefault(field, [])
    if selected:
        for claim in claims:
            claim.is_selected = False
    key = _claim_key(value)
    for claim in claims:
        if _claim_key(claim.value) == key:
            claim.evidence_refs = _merge_refs(claim.evidence_refs, refs)
            if evidence_scope == "field":
                claim.evidence_scope = "field"
            claim.is_selected = claim.is_selected or selected
            return
    claims.append(CandidateFieldClaim(
        value=_json_value(value),
        evidence_refs=list(refs),
        evidence_scope=evidence_scope,
        is_selected=selected,
    ))


def _merge_contacts(selected: list, incoming: list) -> list:
    merged = list(selected)
    seen = {(getattr(item, "type", ""), getattr(item, "value", "")) for item in merged}
    for item in incoming:
        key = (getattr(item, "type", ""), getattr(item, "value", ""))
        if key not in seen:
            merged.append(item)
            seen.add(key)
    return merged


def _merge_dict_field(
    candidate: object,
    field: str,
    incoming: dict,
    selected_source: object,
    incoming_source: object,
) -> None:
    selected = dict(getattr(candidate, field) or {})
    for key, value in incoming.items():
        if key not in selected:
            selected[key] = value
            continue
        if selected[key] != value:
            nested = f"{field}.{key}"
            selected_refs, selected_scope = _value_evidence(selected_source, field, selected[key])
            incoming_refs, incoming_scope = _value_evidence(incoming_source, field, value)
            _record_claim(
                candidate, nested, selected[key], selected_refs,
                selected=True, evidence_scope=selected_scope,
            )
            _record_claim(
                candidate, nested, value, incoming_refs,
                selected=False, evidence_scope=incoming_scope,
            )
            if nested not in candidate.conflicting_fields:
                candidate.conflicting_fields.append(nested)
    setattr(candidate, field, selected)


def _recompute_completeness(candidate: object) -> float:
    has_vendor = bool(candidate.evidence_refs and candidate.vendor_name != "Unknown Vendor")
    if hasattr(candidate, "service_match_evidence"):
        backed = [has_vendor, bool(candidate.service_match_evidence), candidate.quote_channel is not None]
    elif hasattr(candidate, "contacts"):
        backed = [has_vendor, bool(candidate.contacts)]
    else:
        status = getattr(getattr(candidate, "pricing_status", None), "value", "NOT_FOUND")
        backed = [has_vendor, status != "NOT_FOUND", bool(getattr(candidate, "product_url", ""))]
    return round(sum(backed) / len(backed), 3)


def _scope_values(candidate: object) -> dict[str, Any]:
    scope = getattr(candidate, "offer_scope", None)
    return _json_value(scope) if scope is not None else {}


def _scope_scalar(value: Any) -> str:
    if isinstance(value, str):
        return " ".join(value.casefold().split())
    return _claim_key(value)


def _scope_relation(left: dict[str, Any], right: dict[str, Any]) -> str:
    """Return same, distinct, or unresolved for two price-condition scopes."""
    incomplete = False
    for field in _SCOPE_DIMENSIONS:
        left_present = _is_present(left.get(field))
        right_present = _is_present(right.get(field))
        if left_present and right_present:
            if _scope_scalar(left[field]) != _scope_scalar(right[field]):
                return "distinct"
        elif left_present != right_present:
            incomplete = True
    return "unresolved" if incomplete else "same"


def _offer_richness(scope: dict[str, Any]) -> tuple[int, int, int, int]:
    return (
        int(_is_present(scope.get("price"))),
        int(_is_present(scope.get("currency"))),
        int(_is_present(scope.get("unit"))),
        sum(_is_present(value) for value in scope.values()),
    )


def _copy_product_offer(target: object, source: object) -> None:
    for field in _PRODUCT_OFFER_FIELDS:
        setattr(target, field, getattr(source, field))


def _merge_product_offer(merged: object, selected: object, incoming: object) -> None:
    """Choose one complete observation; retain alternatives without mixing fields."""
    selected_scope = _scope_values(selected)
    incoming_scope = _scope_values(incoming)
    selected_refs = list(getattr(selected, "evidence_refs", []) or [])
    incoming_refs = list(getattr(incoming, "evidence_refs", []) or [])
    if selected_scope:
        _record_claim(
            merged, "offer_scope", selected_scope, selected_refs,
            selected=True, evidence_scope="candidate",
        )
    if incoming_scope:
        _record_claim(
            merged, "offer_scope", incoming_scope, incoming_refs,
            selected=False, evidence_scope="candidate",
        )

    chosen = selected
    if (
        _offer_richness(incoming_scope) > _offer_richness(selected_scope)
        or (
            _offer_richness(incoming_scope) == _offer_richness(selected_scope)
            and _claim_key(incoming_scope) < _claim_key(selected_scope)
        )
    ):
        chosen = incoming
        _copy_product_offer(merged, incoming)
    chosen_scope = _scope_values(chosen)
    observations = list(merged.field_claims.get("offer_scope", []))
    for claim in observations:
        claim.is_selected = _claim_key(claim.value) == _claim_key(chosen_scope)

    relations: list[str] = []
    conflicting_assertions: set[str] = set()
    for index, left_claim in enumerate(observations):
        left = left_claim.value
        for right_claim in observations[index + 1:]:
            right = right_claim.value
            relation = _scope_relation(left, right)
            relations.append(relation)
            if relation != "same":
                continue
            for field in ("price", "pricing_status"):
                if (
                    _is_present(left.get(field)) and _is_present(right.get(field))
                    and _claim_key(left[field]) != _claim_key(right[field])
                ):
                    conflicting_assertions.add(field)

    if "distinct" in relations:
        merged.offer_scope_status = "multiple"
    elif "unresolved" in relations:
        merged.offer_scope_status = "unresolved"
    else:
        merged.offer_scope_status = "resolved"

    for field in sorted(conflicting_assertions):
        for observation in observations:
            if _is_present(observation.value.get(field)):
                _record_claim(
                    merged, field, observation.value[field], observation.evidence_refs,
                    selected=observation.is_selected, evidence_scope=observation.evidence_scope,
                )
        if field not in merged.conflicting_fields:
            merged.conflicting_fields.append(field)
    if conflicting_assertions and "offer_scope" not in merged.conflicting_fields:
        merged.conflicting_fields.append("offer_scope")


def _consolidate(selected: object, incoming: object) -> object:
    """Merge observations into a deep copy of the selected representative."""
    merged = selected.model_copy(deep=True)
    merged.evidence_refs = _merge_refs(
        list(getattr(selected, "evidence_refs", []) or []),
        list(getattr(incoming, "evidence_refs", []) or []),
    )
    merged.conflicting_fields = list(dict.fromkeys([
        *getattr(selected, "conflicting_fields", []),
        *getattr(incoming, "conflicting_fields", []),
    ]))
    for field, claims in (getattr(incoming, "field_claims", {}) or {}).items():
        for claim in claims:
            _record_claim(
                merged,
                field,
                claim.value,
                claim.evidence_refs,
                selected=claim.is_selected,
                evidence_scope=claim.evidence_scope,
            )

    is_product = hasattr(merged, "product_name")
    if is_product:
        _merge_product_offer(merged, selected, incoming)

    for field in merged.__class__.model_fields:
        if field in _IDENTITY_FIELDS:
            continue
        if is_product and field in _PRODUCT_OFFER_FIELDS:
            continue
        selected_value = getattr(merged, field)
        incoming_value = getattr(incoming, field)
        if field in _DERIVED_MAX_FIELDS:
            if isinstance(selected_value, bool):
                setattr(merged, field, selected_value or incoming_value)
            elif isinstance(selected_value, (int, float)) and isinstance(incoming_value, (int, float)):
                setattr(merged, field, max(selected_value, incoming_value))
            continue
        if field in _DERIVED_RESET_FIELDS:
            continue
        if field == "contacts":
            setattr(merged, field, _merge_contacts(selected_value, incoming_value))
            continue
        if isinstance(selected_value, dict) and isinstance(incoming_value, dict):
            _merge_dict_field(merged, field, incoming_value, selected, incoming)
            continue
        if not _is_present(incoming_value):
            continue
        incoming_refs, incoming_scope = _value_evidence(incoming, field, incoming_value)
        if not _is_present(selected_value):
            setattr(merged, field, incoming_value)
            _record_claim(
                merged, field, incoming_value, incoming_refs,
                selected=True, evidence_scope=incoming_scope,
            )
            continue
        selected_refs, selected_scope = _value_evidence(selected, field, selected_value)
        _record_claim(
            merged, field, selected_value, selected_refs,
            selected=True, evidence_scope=selected_scope,
        )
        if _claim_key(selected_value) == _claim_key(incoming_value):
            _record_claim(
                merged, field, incoming_value, incoming_refs,
                selected=True, evidence_scope=incoming_scope,
            )
            continue
        _record_claim(
            merged, field, incoming_value, incoming_refs,
            selected=False, evidence_scope=incoming_scope,
        )
        if field not in _MULTIVALUED_FIELDS and field not in merged.conflicting_fields:
            merged.conflicting_fields.append(field)

    if not merged.website and getattr(incoming, "website", None):
        merged.website = incoming.website
    merged.evidence_completeness = _recompute_completeness(merged)
    if hasattr(merged, "checklist_completeness"):
        merged.checklist_completeness = 0.0
    if hasattr(merged, "conflict_penalty"):
        merged.conflict_penalty = 0.0
    merged.score = 0.0
    if hasattr(merged, "score_components"):
        merged.score_components = {}
    ensure_supplier_id(merged)
    ensure_offering_id(merged)
    return merged


def _richness(candidate: object) -> tuple[int, int]:
    refs = len(getattr(candidate, "evidence_refs", []) or [])
    populated = sum(
        1
        for field in candidate.__class__.model_fields
        if field not in _IDENTITY_FIELDS
        and field not in _DERIVED_RESET_FIELDS
        and _is_present(getattr(candidate, field))
    )
    return refs, populated


def dedupe_candidates(candidates: list) -> tuple[list, int]:
    """Resolve identities and consolidate duplicate candidate observations."""
    consolidated: list[object] = []
    merge_count = 0
    for raw in candidates:
        candidate = raw.model_copy(deep=True)
        ensure_supplier_id(candidate)
        ensure_offering_id(candidate)
        match_index = next((
            index for index, existing in enumerate(consolidated)
            if candidates_same_supplier(existing, candidate)
            and candidates_same_offering(existing, candidate)
        ), None)
        if match_index is None:
            consolidated.append(candidate)
            continue
        existing = consolidated[match_index]
        candidate_wins = _richness(candidate) > _richness(existing) or (
            _richness(candidate) == _richness(existing)
            and len(candidate.vendor_name) > len(existing.vendor_name)
        )
        consolidated[match_index] = (
            _consolidate(candidate, existing)
            if candidate_wins else _consolidate(existing, candidate)
        )
        merge_count += 1
    return consolidated, merge_count


__all__ = ["dedupe_candidates", "normalize_vendor_name"]
