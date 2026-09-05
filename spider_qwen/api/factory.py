"""Shared Controller construction for every entrypoint (CLI, HTTP server).

One place wires offline/qwen-json/state-dir so a new provider option lands in
both entrypoints at once instead of drifting apart.
"""

from __future__ import annotations

import os
from copy import deepcopy

from ..agent.controller import Controller


def build_controller(
    *,
    offline: bool,
    state_dir: str | None = None,
    qwen_json: bool = False,
    verify: bool | None = None,
    require_review: bool | None = None,
    rfq_grade_floor: str | None = None,
    expected_config_fingerprint: str | None = None,
) -> Controller:
    """Construct the Controller the same way for the CLI and the HTTP server.

    offline=True is the controller-level guarantee: mock search/fetch AND no
    live Qwen client (router, NLI, extractor, rewriter, drafter), even when
    policy/env flags enable one and an API key is in the env.
    """
    qwen_json_extractor = None
    if qwen_json:
        if offline:
            from ..tools.qwen_json_extractor import MockQwenJsonExtractor

            qwen_json_extractor = MockQwenJsonExtractor()
        else:
            from ..tools.qwen_json_extractor import QwenJsonExtractor

            qwen_json_extractor = QwenJsonExtractor()
    policy = None
    if rfq_grade_floor is not None:
        from ..agent.policy import Policy, load_policy
        data = deepcopy(load_policy().data)
        data.setdefault("rfq", {})["grade_floor"] = rfq_grade_floor
        policy = Policy(data)
    conformal = None
    if verify is True:
        from ..application.profiles import PIPELINE_VERSION
        from ..verification.conformal import gate_from_env
        conformal = gate_from_env(
            expected_pipeline_version=PIPELINE_VERSION,
            expected_config_fingerprint=expected_config_fingerprint,
        )
    return Controller(
        policy=policy,
        conformal=conformal,
        qwen_json_extractor=qwen_json_extractor,
        state_dir=state_dir or os.getenv("SPIDER_QWEN_STATE_DIR", ".spider_qwen"),
        verify=verify,
        require_review=require_review,
        offline=offline,
    )


def build_run_service(
    *,
    state_dir: str | None = None,
    allow_live: bool = False,
    max_concurrency: int = 2,
    max_queued: int = 8,
    max_live_concurrency: int = 1,
    max_live_runs_per_utc_day: int = 20,
    max_deadline_seconds: int = 900,
):
    """Build the shared durable local execution service."""
    from ..application.run_service import RunService

    return RunService(
        state_dir=state_dir or os.getenv("SPIDER_QWEN_STATE_DIR", ".spider_qwen"),
        controller_builder=build_controller,
        allow_live=allow_live,
        max_concurrency=max_concurrency,
        max_queued=max_queued,
        max_live_concurrency=max_live_concurrency,
        max_live_runs_per_utc_day=max_live_runs_per_utc_day,
        max_deadline_seconds=max_deadline_seconds,
    )
