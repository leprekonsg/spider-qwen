"""Privacy classification for extracted contact data."""

from __future__ import annotations

from ..modes.contracts import PrivacyClass

_HIGH_SENSITIVITY_FIELDS = {"named_person_email", "named_person_phone", "direct_mobile"}


def classify_field_privacy(field: str, privacy_class: PrivacyClass | None = None) -> PrivacyClass:
    if privacy_class is not None:
        return privacy_class
    if field in _HIGH_SENSITIVITY_FIELDS:
        return PrivacyClass.NAMED_PERSON_HIGH_SENSITIVITY
    return PrivacyClass.BUSINESS_CONTACT


def is_high_sensitivity(privacy_class: PrivacyClass) -> bool:
    return privacy_class == PrivacyClass.NAMED_PERSON_HIGH_SENSITIVITY
