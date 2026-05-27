"""Governance: privacy tagging, review gates, audit trail.

v1 position: tag named-person data as high sensitivity; review gates are
configurable but default to disabled. RFQ submission is never allowed.
"""

from __future__ import annotations

from .privacy import classify_field_privacy, is_high_sensitivity
from .review import ReviewGate
from .audit import AuditLog, AuditEvent

__all__ = [
    "classify_field_privacy",
    "is_high_sensitivity",
    "ReviewGate",
    "AuditLog",
    "AuditEvent",
]
