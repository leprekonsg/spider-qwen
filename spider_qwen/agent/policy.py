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
from ..observability.metrics import RouteDecision

_DEFAULT_PATH = Path(__file__).resolve().parent.parent / "governance" / "policy_config.yaml"

# Env override per model role: SPIDER_QWEN_MODEL_<ROLE>.
_MODEL_ENV = {
    "planner": "SPIDER_QWEN_MODEL_PLANNER",
    "extraction": "SPIDER_QWEN_MODEL_EXTRACTION",
    "extraction_fallback": "SPIDER_QWEN_MODEL_EXTRACTION_FALLBACK",
    "embeddings": "SPIDER_QWEN_MODEL_EMBEDDINGS",
    "ocr": "SPIDER_QWEN_MODEL_OCR",
}

# T-7.3 cost router. Cheap, high-volume steps -> flash; planning/reasoning ->
# max. The high_risk_procurement tag escalates any step to max.
_TASK_TIER = {
    "classification": "flash",
    "extraction": "flash",
    "extraction_fallback": "flash",
    "judge": "flash",
    "reflection": "flash",
    "query_expansion": "flash",
    "decision": "flash",
    "planning": "max",
    "reasoning": "max",
}
_TIER_ROLE = {"flash": "extraction", "max": "planner"}

# Illustrative USD per 1K tokens; override per model via policy_config.yaml
# `pricing:`. Flash is far cheaper than max, which is what makes the router pay.
DEFAULT_MODEL_PRICING = {
    "qwen3.7-max": {"input": 0.0024, "output": 0.0096},
    "qwen3.5-flash": {"input": 0.0003, "output": 0.0006},
    "qwen-flash": {"input": 0.0002, "output": 0.0004},
    "text-embedding-v4": {"input": 0.00007, "output": 0.0},
    "qwen-vl-ocr-2025-11-20": {"input": 0.0010, "output": 0.0010},
}


class Policy:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    # --- model roles ------------------------------------------------------
    @property
    def models(self) -> dict[str, str]:
        return dict(self.data.get("models", {}))

    def model_for(self, role: str) -> str:
        """Resolve the Qwen model string for a role (planner|extraction|...).

        Precedence: env SPIDER_QWEN_MODEL_<ROLE> > models.<role> in config.
        Raises KeyError with an actionable message when the role is unconfigured.
        """
        env = _MODEL_ENV.get(role)
        if env:
            override = os.getenv(env)
            if override:
                return override
        models = self.data.get("models", {})
        value = models.get(role)
        if value:
            return str(value)
        hint = f" or set {env}" if env else ""
        raise KeyError(
            f"No Qwen model configured for role '{role}'. "
            f"Add 'models.{role}: <model>' to policy_config.yaml{hint}."
        )

    # --- cost router (T-7.3) ----------------------------------------------
    def route_task(self, task: str, *, high_risk: bool = False) -> RouteDecision:
        """Pick the model tier for a logical step.

        extraction/classification/judge -> flash; planning/reasoning -> max; an
        unknown step defaults to flash. The high_risk_procurement tag escalates
        any step to max.
        """
        base = _TASK_TIER.get(task, "flash")
        tier = "max" if (high_risk or base == "max") else "flash"
        role = _TIER_ROLE[tier]
        try:
            model = self.model_for(role)
        except KeyError:
            model = ""  # routing decision still stands when no model is configured
        return RouteDecision(
            task=task, tier=tier, role=role, model=model,
            escalated=high_risk and base != "max",
        )

    def model_pricing(self) -> dict[str, dict[str, float]]:
        """Per-model {input, output} USD/1K-token table; yaml overrides defaults."""
        merged = {k: dict(v) for k, v in DEFAULT_MODEL_PRICING.items()}
        for model, price in (self.data.get("pricing", {}) or {}).items():
            merged[str(model)] = {**merged.get(str(model), {}), **{k: float(v) for k, v in (price or {}).items()}}
        return merged

    def validate_model_ids(self) -> None:
        """Fail loud when a Qwen call path resolves to an unpinned model id.

        The ``pricing:`` block doubles as the pinned known-good model set: every
        id a live path can call must be priced there. Catches typos in env
        overrides (e.g. QWEN_ROUTER_MODEL) at init instead of mid-run.
        """
        pricing = self.model_pricing()
        resolved: dict[str, str] = {}
        for role in self.models:
            try:
                resolved[f"models.{role}"] = self.model_for(role)
            except KeyError:
                continue
        for source, getter in (
            ("qwen_router_model", self.qwen_router_model),
            ("qwen_json_extractor_model", self.qwen_json_extractor_model),
            ("qwen_nli_model", self.qwen_nli_model),
            ("qwen_query_rewriter_model", self.qwen_query_rewriter_model),
            ("qwen_rfq_drafter_model", self.qwen_rfq_drafter_model),
        ):
            try:
                resolved[source] = getter()
            except KeyError:
                continue
        unknown = {src: mid for src, mid in resolved.items() if mid and mid not in pricing}
        if unknown:
            offenders = ", ".join(f"{src}={mid!r}" for src, mid in sorted(unknown.items()))
            raise ValueError(
                f"Unknown Qwen model id(s): {offenders}. Pin each id in the "
                "pricing: block of governance/policy_config.yaml (the known-good "
                "model set), or fix the model id / env override."
            )

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
        # env > legacy qwen.router_model > canonical models.planner.
        return (
            os.getenv("QWEN_ROUTER_MODEL")
            or str(self.data.get("qwen", {}).get("router_model") or "")
            or self.model_for("planner")
        )

    def qwen_json_extractor_model(self) -> str:
        # env > legacy qwen.json_extractor_model > canonical models.extraction.
        return (
            os.getenv("QWEN_JSON_EXTRACTOR_MODEL")
            or str(self.data.get("qwen", {}).get("json_extractor_model") or "")
            or self.model_for("extraction")
        )

    def qwen_structured_extraction_enabled(self) -> bool:
        return _env_bool("QWEN_STRUCTURED_EXTRACTION_ENABLED", self.data.get("qwen", {}).get("structured_extraction_enabled", False))

    def qwen_router_fallback_enabled(self) -> bool:
        return _env_bool("QWEN_ROUTER_FALLBACK_ENABLED", self.data.get("qwen", {}).get("router_fallback_enabled", False))

    def qwen_page_judge_enabled(self) -> bool:
        return _env_bool("QWEN_PAGE_JUDGE_ENABLED", self.data.get("qwen", {}).get("page_judge_enabled", False))

    def qwen_nli_enabled(self) -> bool:
        # T-2.2 seam: Qwen scores (claim, span) entailment inside MiniCheck.
        # Off by default; the deterministic heuristic keeps final authority.
        return _env_bool("QWEN_NLI_ENABLED", self.data.get("qwen", {}).get("nli_enabled", False))

    def qwen_nli_model(self) -> str:
        return (
            os.getenv("QWEN_NLI_MODEL")
            or str(self.data.get("qwen", {}).get("nli_model") or "")
            or "qwen-flash"
        )

    def qwen_query_rewriter_enabled(self) -> bool:
        # CRAG-style corrective retrieval (Yan et al. 2024): Qwen proposes the
        # rewritten pivot queries; retrieval/extraction stay deterministic.
        return _env_bool("QWEN_QUERY_REWRITER_ENABLED", self.data.get("qwen", {}).get("query_rewriter_enabled", False))

    def qwen_query_rewriter_model(self) -> str:
        return (
            os.getenv("QWEN_QUERY_REWRITER_MODEL")
            or str(self.data.get("qwen", {}).get("query_rewriter_model") or "")
            or "qwen-flash"
        )

    def qwen_rfq_drafter_enabled(self) -> bool:
        # CoVe-style split (Dhuliawala et al. 2023): Qwen drafts the RFQ body;
        # a deterministic fact-check flags unsourced claims against the ledger.
        return _env_bool("QWEN_RFQ_DRAFTER_ENABLED", self.data.get("qwen", {}).get("rfq_drafter_enabled", False))

    def qwen_rfq_drafter_model(self) -> str:
        return (
            os.getenv("QWEN_RFQ_DRAFTER_MODEL")
            or str(self.data.get("qwen", {}).get("rfq_drafter_model") or "")
            or self.model_for("planner")
        )

    def verification_enabled(self) -> bool:
        # T-2.2 verification spine. Off by default; the deterministic gatekeeper
        # blocks candidates whose critical claims are not grounded in evidence.
        return _env_bool("SPIDER_QWEN_VERIFICATION_ENABLED", self.data.get("verification", {}).get("enabled", False))

    def source_reliability(self) -> dict[str, float]:
        # T-2.4 per-source reliability priors; yaml overrides the code defaults.
        from ..governance.source_reliability import DEFAULT_RELIABILITY

        overrides = self.data.get("source_reliability", {}) or {}
        return {**DEFAULT_RELIABILITY, **{k: float(v) for k, v in overrides.items()}}

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
