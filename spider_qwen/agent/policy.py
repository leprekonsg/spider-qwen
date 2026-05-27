"""Policy config loader.

Loads governance/policy_config.yaml into typed accessors. Controls budgets, geo
defaults, privacy tags, RFQ behavior, and memory rules. Everything advanced
stays disabled by default in v1.
"""

from __future__ import annotations

import os
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

    def qwen_router_model(self) -> str:
        return os.getenv("QWEN_ROUTER_MODEL") or str(self.data.get("qwen", {}).get("router_model", "qwen3-max-2026-01-23"))

    def qwen_json_extractor_model(self) -> str:
        return os.getenv("QWEN_JSON_EXTRACTOR_MODEL") or str(self.data.get("qwen", {}).get("json_extractor_model", "qwen-flash"))

    def qwen_structured_extraction_enabled(self) -> bool:
        return _env_bool("QWEN_STRUCTURED_EXTRACTION_ENABLED", self.data.get("qwen", {}).get("structured_extraction_enabled", False))

    def qwen_router_fallback_enabled(self) -> bool:
        return _env_bool("QWEN_ROUTER_FALLBACK_ENABLED", self.data.get("qwen", {}).get("router_fallback_enabled", False))

    def qwen_router_confidence_threshold(self) -> float:
        return float(os.getenv("QWEN_ROUTER_CONFIDENCE_THRESHOLD") or self.data.get("qwen", {}).get("router_confidence_threshold", 0.65))

    def hitl_enabled(self) -> bool:
        return bool(self.data.get("hitl", {}).get("enabled", False))

    def hitl_require_review(self) -> bool:
        return bool(self.data.get("hitl", {}).get("require_review", False))

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


def _env_bool(name: str, default: Any) -> bool:
    value = os.getenv(name)
    if value is None:
        return bool(default)
    return value.lower() in {"1", "true", "yes", "on"}
