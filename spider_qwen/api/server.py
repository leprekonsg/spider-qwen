"""Optional HTTP API (FastAPI).

Install with: pip install 'spider-qwen[server]'
Run with:     uvicorn spider_qwen.api.server:app

Kept thin: one /classify and one /run endpoint over the same Controller used by
the CLI. FastAPI is imported lazily so the package works without it.
"""

from __future__ import annotations

import os


def create_app():
    try:
        from fastapi import FastAPI
        from pydantic import BaseModel
    except ImportError as exc:  # pragma: no cover - optional dep
        raise RuntimeError(
            "FastAPI not installed. Install with: pip install 'spider-qwen[server]'"
        ) from exc

    from ..agent.controller import Controller
    from ..modes.classifier import ModeClassifier

    app = FastAPI(title="spider-qwen", version="0.1.0")
    state_dir = os.getenv("SPIDER_QWEN_STATE_DIR", ".spider_qwen")

    class RunRequest(BaseModel):
        query: str
        mode: str = "auto"
        country: str | None = None

    @app.post("/classify")
    def classify(req: RunRequest):
        return ModeClassifier().classify(req.query).model_dump()

    @app.post("/run")
    async def run(req: RunRequest):
        controller = Controller(state_dir=state_dir)
        result = await controller.run(req.query, mode=req.mode, target_country=req.country)
        return result.model_dump(mode="json")

    return app


# Lazily constructed app for `uvicorn spider_qwen.api.server:app`.
try:  # pragma: no cover - import-time convenience
    app = create_app()
except RuntimeError:
    app = None
