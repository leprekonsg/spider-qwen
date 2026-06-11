"""Shared Controller construction for every entrypoint (CLI, HTTP server).

One place wires offline/qwen-json/state-dir so a new provider option lands in
both entrypoints at once instead of drifting apart.
"""

from __future__ import annotations

import os

from ..agent.controller import Controller


def build_controller(
    *,
    offline: bool,
    state_dir: str | None = None,
    qwen_json: bool = False,
    verify: bool | None = None,
    require_review: bool | None = None,
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
    return Controller(
        qwen_json_extractor=qwen_json_extractor,
        state_dir=state_dir or os.getenv("SPIDER_QWEN_STATE_DIR", ".spider_qwen"),
        verify=verify,
        require_review=require_review,
        offline=offline,
    )
