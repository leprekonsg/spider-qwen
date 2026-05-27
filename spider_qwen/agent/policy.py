"""Policy config loader.

Loads governance/policy_config.yaml into typed accessors. Controls budgets, geo
defaults, privacy tags, RFQ behavior, and memory rules. Everything advanced
stays disabled by default in v1.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .budget import Budget
from ..modes.contracts import ProcurementMode

_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "governance" / "policy_config.yaml"


class Policy:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    @property
    def schema_version(self) -> str:
        return self.data.get("schema_version", "1.0")

    @property
    def geo(self) -> dict[str, Any]:
        return self.data.get("geo", {})

    @property
    def default_region(self) -> str:
        return self.geo.get("default_region", "SEA")

    @property
    def fallback_region(self) -> str:
        return self.geo.get("fallback_region", "global")

    @property
    def boost_countries(self) -> list[str]:
        return list(self.geo.get("boost_countries", []))

    @property
    def allow_vendor_submission(self) -> bool:
        # v1 hard rule: RFQ drafts are never submitted, even if config drifts.
        return False

    @property
    def rfq_tone(self) -> str:
        return self.data.get("rfq", {}).get(
            "default_tone", "SEA-neutral professional English; short and direct"
        )

    @property
    def minimum_checklist_completeness(self) -> float:
        return float(self.data.get("rfq", {}).get("minimum_checklist_completeness", 0.65))

    @property
    def allow_disputed_facts_in_rfq(self) -> bool:
        return bool(self.data.get("memory", {}).get("allow_disputed_facts_in_rfq", False))

    @property
    def semantic_promotion_requires_evidence(self) -> bool:
        return bool(self.data.get("memory", {}).get("semantic_promotion_requires_evidence", True))

    def review_gate_enabled(self, privacy_class: str) -> bool:
        return bool(self.data.get("privacy", {}).get("review_gate_enabled", {}).get(privacy_class, False))

    def high_sensitivity_fields(self) -> list[str]:
        return list(self.data.get("privacy", {}).get("high_sensitivity_fields", []))

    def budget_for(self, mode: ProcurementMode, budget_key: str | None = None) -> Budget:
        key = budget_key or mode.value
        raw = self.data.get("budgets", {}).get(key, {})
        return Budget(mode=mode.value, **raw)


def load_policy(path: str | Path | None = None) -> Policy:
    target = Path(path) if path else _DEFAULT_PATH
    data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    return Policy(data)
