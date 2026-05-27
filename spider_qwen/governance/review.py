"""Configurable review gate. Disabled by default in v1 (see policy_config.yaml)."""

from __future__ import annotations

from ..modes.contracts import PrivacyClass


class ReviewGate:
    def __init__(self, policy: object) -> None:
        self.policy = policy

    def requires_review(self, privacy_class: PrivacyClass) -> bool:
        gate = getattr(self.policy, "review_gate_enabled", None)
        if callable(gate):
            return bool(gate(privacy_class.value))
        return False
