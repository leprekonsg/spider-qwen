"""Operator-controlled execution profiles.

Profiles deliberately contain only settings enforced by the current controller.
The persisted effective configuration is therefore an honest record of a run,
not a promise about controls that the process does not implement.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass

from .. import SCHEMA_VERSION, __version__


PIPELINE_VERSION = (
    f"spider-qwen/{__version__};schema={SCHEMA_VERSION};"
    "identity=2;ranking=2;profiles=1;execution=1;verification=2"
)


@dataclass(frozen=True)
class OperatorProfile:
    name: str
    offline: bool
    verify: bool
    require_review: bool
    qwen_json: bool
    rfq_grade_floor: str
    default_deadline_seconds: int
    description: str

    @property
    def live(self) -> bool:
        return not self.offline

    def effective_config(self, *, deadline_seconds: int | None = None) -> dict:
        value = asdict(self)
        value["deadline_seconds"] = deadline_seconds or self.default_deadline_seconds
        value["provider_mode"] = "mock" if self.offline else "live"
        value["pipeline_version"] = PIPELINE_VERSION
        from ..agent.policy import load_policy

        policy = load_policy()
        model_getters = {
            "router": policy.qwen_router_model,
            "structured_extraction": policy.qwen_json_extractor_model,
            "nli": policy.qwen_nli_model,
            "query_rewriter": policy.qwen_query_rewriter_model,
            "rfq_drafter": policy.qwen_rfq_drafter_model,
            "frontier_scorer": policy.qwen_frontier_scorer_model,
        }
        models = {}
        for role in policy.models:
            try:
                models[f"role:{role}"] = policy.model_for(role)
            except KeyError:
                models[f"role:{role}"] = None
        for role, getter in model_getters.items():
            try:
                models[role] = getter()
            except KeyError:
                models[role] = None
        effective_policy = {
            "policy_source_sha256": hashlib.sha256(
                json.dumps(policy.data, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            "models": models,
            "providers": {
                "search": "mock" if self.offline else os.getenv("SPIDER_QWEN_SEARCH_PROVIDER", "tinyfish"),
                "fetch": "mock" if self.offline else os.getenv("SPIDER_QWEN_FETCH_PROVIDER", "tinyfish"),
            },
            "features": {
                "verification": self.verify,
                "structured_extraction": self.qwen_json or policy.qwen_structured_extraction_enabled(),
                "router_fallback": policy.qwen_router_fallback_enabled(),
                "page_judge": policy.qwen_page_judge_enabled(),
                "fetch_fallback": policy.qwen_fetch_fallback_enabled(),
                "nli": policy.qwen_nli_enabled(),
                "query_rewriter": policy.qwen_query_rewriter_enabled(),
                "rfq_drafter": policy.qwen_rfq_drafter_enabled(),
                "frontier": policy.frontier_enabled(),
                "frontier_scorer": policy.qwen_frontier_scorer_enabled(),
                "page_cache": policy.page_cache_enabled(),
                "mandatory_review": self.require_review,
                "rfq_grade_floor": self.rfq_grade_floor,
            },
        }
        value["policy"] = effective_policy
        value["config_fingerprint"] = hashlib.sha256(
            json.dumps(
                value,
                sort_keys=True, separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return value


_PROFILES = {
    "offline_demo": OperatorProfile(
        name="offline_demo",
        offline=True,
        verify=False,
        require_review=False,
        qwen_json=False,
        rfq_grade_floor="very_low",
        default_deadline_seconds=120,
        description="Deterministic fixture providers; no network or model spending.",
    ),
    "live_research": OperatorProfile(
        name="live_research",
        offline=False,
        verify=True,
        require_review=False,
        qwen_json=False,
        rfq_grade_floor="low",
        default_deadline_seconds=300,
        description="Live acquisition with critical-claim verification; no supplier submission.",
    ),
    "reviewed_procurement": OperatorProfile(
        name="reviewed_procurement",
        offline=False,
        verify=True,
        require_review=True,
        qwen_json=False,
        rfq_grade_floor="moderate",
        default_deadline_seconds=300,
        description="Live verified research with mandatory review before operational use.",
    ),
}


def get_profile(name: str) -> OperatorProfile:
    try:
        return _PROFILES[name]
    except KeyError as exc:
        choices = ", ".join(_PROFILES)
        raise ValueError(f"Unknown operator profile '{name}'. Choose one of: {choices}.") from exc


def list_profiles() -> list[dict]:
    return [profile.effective_config() for profile in _PROFILES.values()]
