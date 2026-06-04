"""T-0: model role configuration block + provider wiring.

The build plan (section 2) requires a `models:` block in policy_config.yaml that
maps each Qwen role (planner, extraction, embeddings, ocr) to a model string,
never hard-coded, and read by provider construction. Env vars override config.
"""

from __future__ import annotations

import pytest

from spider_qwen.agent.policy import Policy, load_policy

REQUIRED_ROLES = ["planner", "extraction", "embeddings", "ocr"]


def test_models_block_defines_required_roles():
    policy = load_policy()
    for role in REQUIRED_ROLES:
        assert policy.model_for(role), f"models.{role} must resolve to a model string"


def test_planner_is_qwen_max_family():
    assert "max" in load_policy().model_for("planner").lower()


def test_extraction_is_flash_family():
    # Cheap extraction/classification routes to a flash model (thinking off).
    assert "flash" in load_policy().model_for("extraction").lower()


def test_embeddings_and_ocr_named():
    policy = load_policy()
    assert "embedding" in policy.model_for("embeddings").lower()
    assert "ocr" in policy.model_for("ocr").lower()


def test_model_for_unknown_role_raises_actionable_error():
    with pytest.raises(KeyError) as exc:
        load_policy().model_for("nonexistent-role")
    assert "nonexistent-role" in str(exc.value)


def test_role_env_override(monkeypatch):
    monkeypatch.setenv("SPIDER_QWEN_MODEL_PLANNER", "qwen-test-override")
    assert load_policy().model_for("planner") == "qwen-test-override"


def test_provider_construction_reads_models_block():
    # With the default config (no QWEN_*_MODEL env, no legacy qwen.*_model keys),
    # provider model resolution falls through to the canonical models: block.
    policy = load_policy()
    assert policy.qwen_router_model() == policy.model_for("planner")
    assert policy.qwen_json_extractor_model() == policy.model_for("extraction")


def test_legacy_env_override_still_wins(monkeypatch):
    monkeypatch.setenv("QWEN_JSON_EXTRACTOR_MODEL", "qwen-legacy-flash")
    assert load_policy().qwen_json_extractor_model() == "qwen-legacy-flash"


def test_legacy_config_key_overrides_models_block():
    # A config that still sets qwen.router_model keeps working (backward compat).
    policy = Policy({"qwen": {"router_model": "qwen-legacy-router"},
                     "models": {"planner": "qwen3.7-max"}})
    assert policy.qwen_router_model() == "qwen-legacy-router"
