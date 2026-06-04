"""Optional HTTP API (FastAPI).

Install with: pip install 'spider-qwen[server]'
Run with:     uvicorn spider_qwen.api.server:app

Kept thin: one /classify and one /run endpoint over the same Controller used by
the CLI. FastAPI is imported lazily so the package works without it.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel  # core dependency; FastAPI stays lazy below

# Repo-root web/ holds the static frontend (index.html + JSX components).
WEB_DIR = Path(__file__).resolve().parents[2] / "web"


class RunRequest(BaseModel):
    query: str
    mode: str = "auto"
    country: str | None = None
    offline: bool = True


def create_app():
    try:
        from fastapi import FastAPI, HTTPException
        from fastapi.staticfiles import StaticFiles
    except ImportError as exc:  # pragma: no cover - optional dep
        raise RuntimeError(
            "FastAPI not installed. Install with: pip install 'spider-qwen[server]'"
        ) from exc

    from ..agent.controller import Controller
    from ..modes.classifier import ModeClassifier

    app = FastAPI(title="spider-qwen", version="0.1.0")
    state_dir = os.getenv("SPIDER_QWEN_STATE_DIR", ".spider_qwen")
    # Secure default: the HTTP server runs offline (deterministic mocks). Live web
    # access (token spend + outbound fetch / SSRF surface) is an operator opt-in,
    # never something a client can switch on. The exposed server has no auth or
    # rate limiting -- if you set this, put it behind auth and a rate limiter.
    allow_live = os.getenv("SPIDER_QWEN_ALLOW_LIVE", "").lower() in {"1", "true", "yes", "on"}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/classify")
    def classify(req: RunRequest):
        return ModeClassifier().classify(req.query).model_dump()

    @app.post("/run")
    async def run(req: RunRequest):
        if not req.offline and not allow_live:
            raise HTTPException(
                status_code=403,
                detail="Live providers are disabled. Set SPIDER_QWEN_ALLOW_LIVE=1 on the server to enable.",
            )
        offline = req.offline or not allow_live
        # offline=True is the controller-level guarantee: mock search/fetch AND
        # no env-driven live Qwen wiring (router, NLI, JSON extractor).
        controller = Controller(state_dir=state_dir, offline=offline)
        result = await controller.run(req.query, mode=req.mode, target_country=req.country)
        return result.model_dump(mode="json")

    # Serve the static frontend at / (mounted last so API routes win). Same
    # origin as the API, so no CORS is needed. html=True serves index.html.
    if WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")

    return app


# Lazily constructed app for `uvicorn spider_qwen.api.server:app`.
try:  # pragma: no cover - import-time convenience
    app = create_app()
except RuntimeError:
    app = None
