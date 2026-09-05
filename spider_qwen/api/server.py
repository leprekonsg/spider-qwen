"""Optional FastAPI adapter over shared application services."""

import asyncio
import hmac
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..application.profiles import get_profile, list_profiles
from ..application.run_service import AdmissionRejected, IdempotencyConflict, RunNotFound, RunNotReady
from ..requirements import Requirement

WEB_DIR = Path(__file__).resolve().parents[2] / "web"
Mode = Literal[
    "auto", "product_exact_price", "service_quote_required",
    "contact_enrichment_only", "revalidation", "electronics_substitution",
]


class RunRequest(BaseModel):
    query: str
    requirements: list[Requirement] = Field(default_factory=list, max_length=100)
    requirements_confirmed: bool = False
    supplier_sources: dict[str, list[str]] = Field(default_factory=dict, max_length=100)
    mode: Mode = "auto"
    country: str | None = None
    offline: bool = True


class StartRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=10_000)
    requirements: list[Requirement] = Field(default_factory=list, max_length=100)
    requirements_confirmed: bool = False
    supplier_sources: dict[str, list[str]] = Field(default_factory=dict, max_length=100)
    mode: Mode = "auto"
    country: str | None = Field(default=None, max_length=100)
    profile: Literal["offline_demo", "live_research", "reviewed_procurement"] | None = None
    offline: bool | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)
    deadline_seconds: int | None = Field(default=None, ge=1)
    high_risk: bool = False
    serendipity: bool = False


@dataclass(frozen=True)
class _Principal:
    owner: str
    authenticated: bool


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        return default if raw is None else int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer; got {raw!r}.") from exc


def _token_owners() -> dict[str, str]:
    raw = os.getenv("SPIDER_QWEN_API_TOKENS", "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("SPIDER_QWEN_API_TOKENS must be a JSON token-to-owner object.") from exc
    valid = isinstance(value, dict) and value and all(
        isinstance(token, str) and token and isinstance(owner, str) and owner
        for token, owner in value.items()
    )
    if not valid:
        raise RuntimeError("SPIDER_QWEN_API_TOKENS must map non-empty token strings to owners.")
    return value


def create_app(*, run_service=None):
    try:
        from fastapi import Depends, FastAPI, Header, HTTPException, Request
        from fastapi.responses import JSONResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("FastAPI not installed. Install with: pip install 'spider-qwen[server]'") from exc

    from ..modes.classifier import ModeClassifier
    from .factory import build_run_service
    from .run_queries import register_query_routes

    state_dir = os.getenv("SPIDER_QWEN_STATE_DIR", ".spider_qwen")
    allow_live = os.getenv("SPIDER_QWEN_ALLOW_LIVE", "").lower() in {"1", "true", "yes", "on"}
    trust_owner_header = os.getenv("SPIDER_QWEN_TRUST_OWNER_HEADER", "").lower() in {
        "1", "true", "yes", "on",
    }
    default_profile = os.getenv("SPIDER_QWEN_DEFAULT_PROFILE", "offline_demo")
    try:
        get_profile(default_profile)
    except ValueError as exc:
        raise RuntimeError(f"Invalid SPIDER_QWEN_DEFAULT_PROFILE: {exc}") from exc
    token_owners = _token_owners()
    owns_service = run_service is None
    service = run_service or build_run_service(
        state_dir=state_dir,
        allow_live=allow_live,
        max_concurrency=_env_int("SPIDER_QWEN_MAX_CONCURRENCY", 2),
        max_queued=_env_int("SPIDER_QWEN_MAX_QUEUED", 8),
        max_live_concurrency=_env_int("SPIDER_QWEN_MAX_LIVE_CONCURRENCY", 1),
        max_live_runs_per_utc_day=_env_int("SPIDER_QWEN_MAX_LIVE_RUNS_PER_UTC_DAY", 20),
        max_deadline_seconds=_env_int("SPIDER_QWEN_MAX_DEADLINE_SECONDS", 900),
    )
    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            if owns_service:
                service.close(wait=True)

    app = FastAPI(title="spider-qwen", version="0.1.0", lifespan=lifespan)
    app.state.run_service = service

    def resolve_principal(request: Request) -> _Principal:
        authorization = request.headers.get("authorization", "")
        if authorization:
            scheme, _, credential = authorization.partition(" ")
            if scheme.lower() != "bearer" or not credential:
                raise HTTPException(status_code=401, detail="Use Authorization: Bearer <token>.")
            owner = next((mapped for token, mapped in token_owners.items()
                          if hmac.compare_digest(credential, token)), None)
            if owner is None:
                raise HTTPException(status_code=401, detail="Bearer token is invalid.")
            return _Principal(owner=owner, authenticated=True)
        if trust_owner_header:
            owner = request.headers.get("x-spider-qwen-owner", "").strip()
            if not owner:
                raise HTTPException(status_code=401, detail="Trusted proxy mode requires X-Spider-Qwen-Owner.")
            return _Principal(owner=owner, authenticated=True)
        if token_owners:
            raise HTTPException(status_code=401, detail="Authorization: Bearer <token> is required.")
        return _Principal(owner="local", authenticated=False)

    def owner_dependency(request: Request) -> str:
        return resolve_principal(request).owner

    for error_type, status_code in (
        (RunNotFound, 404), (RunNotReady, 409), (IdempotencyConflict, 409),
        (AdmissionRejected, 429),
    ):
        def handler(_request, exc, code=status_code):
            headers = {"Retry-After": "1"} if code == 429 else None
            return JSONResponse(status_code=code, content={"detail": str(exc)}, headers=headers)
        app.add_exception_handler(error_type, handler)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/config")
    def config():
        return {
            "execution": "local", "distributed_scheduler": False,
            "allow_live": allow_live, "default_profile": default_profile,
            "profiles": list_profiles(),
            "limits": {
                "max_concurrency": service.max_concurrency, "max_queued": service.max_queued,
                "max_live_concurrency": service.max_live_concurrency,
                "max_live_runs_per_utc_day": service.max_live_runs_per_utc_day,
                "live_admission_unit": "started live runs per UTC day (no refunds)",
                "max_deadline_seconds": service.max_deadline_seconds,
            },
            "auth_mode": "trusted_proxy" if trust_owner_header else (
                "bearer" if token_owners else "local_only"
            ),
        }

    @app.post("/classify")
    def classify(req: RunRequest):
        return ModeClassifier().classify(req.query).model_dump()

    @app.post("/runs", status_code=202)
    def start_run(req: StartRunRequest, request: Request,
                  idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        principal = resolve_principal(request)
        profile = req.profile or (
            ("offline_demo" if req.offline else "live_research")
            if req.offline is not None else default_profile
        )
        if idempotency_key and req.idempotency_key and idempotency_key != req.idempotency_key:
            raise HTTPException(status_code=422, detail="Header and body idempotency keys must match.")
        if default_profile == "reviewed_procurement" and profile == "live_research":
            raise HTTPException(
                status_code=403,
                detail="Operator policy requires reviewed_procurement for live runs; "
                       "choose reviewed_procurement or offline_demo.",
            )
        if get_profile(profile).live and not service.allow_live:
            raise HTTPException(
                status_code=403,
                detail="Live profiles are disabled. Set SPIDER_QWEN_ALLOW_LIVE=1 on the server to enable them.",
            )
        if get_profile(profile).live and not principal.authenticated:
            raise HTTPException(status_code=401, detail="Live profiles require an authenticated principal.")
        try:
            payload = req.model_dump(exclude={"offline", "idempotency_key"})
            return service.start({**payload, "profile": profile}, owner=principal.owner,
                                 idempotency_key=idempotency_key or req.idempotency_key)
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/runs/{run_id}")
    def run_status(run_id: str, owner: str = Depends(owner_dependency)):
        return service.status(run_id, owner=owner)

    @app.get("/runs/{run_id}/result")
    def run_result(run_id: str, owner: str = Depends(owner_dependency)):
        return service.result(run_id, owner=owner)

    @app.get("/runs/{run_id}/events")
    def run_events(run_id: str, after: int = 0, limit: int = 100,
                   owner: str = Depends(owner_dependency)):
        return {"events": service.events(run_id, owner=owner, after_id=after, limit=limit)}

    @app.post("/runs/{run_id}/cancel", status_code=202)
    def cancel_run(run_id: str, owner: str = Depends(owner_dependency)):
        return service.cancel(run_id, owner=owner)

    @app.post("/run")
    async def legacy_run(req: RunRequest, request: Request):
        principal = resolve_principal(request)
        profile = "offline_demo" if req.offline else (
            "reviewed_procurement" if default_profile == "reviewed_procurement" else "live_research"
        )
        if not req.offline and not service.allow_live:
            raise HTTPException(
                status_code=403,
                detail="Live providers are disabled. Set SPIDER_QWEN_ALLOW_LIVE=1 on the server to enable.",
            )
        if not req.offline and not principal.authenticated:
            raise HTTPException(status_code=401, detail="Live runs require an authenticated principal.")
        try:
            started = service.start(
                {"query": req.query, "mode": req.mode, "country": req.country, "profile": profile,
                 "requirements": [r.model_dump() for r in req.requirements],
                 "requirements_confirmed": req.requirements_confirmed, "supplier_sources": req.supplier_sources},
                owner=principal.owner,
            )
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        await asyncio.to_thread(service.wait, started["run_id"], owner=principal.owner)
        final = service.status(started["run_id"], owner=principal.owner)
        if final["status"] != "completed":
            code = 504 if final["status"] == "timed_out" else 500
            raise HTTPException(status_code=code, detail=final["error"] or final["status"])
        return service.result(started["run_id"], owner=principal.owner)

    register_query_routes(app, service, owner_dependency)
    if WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
    return app


class _LazyApp:
    """Delay state ownership until an ASGI server actually starts the app."""

    def __init__(self):
        self._app = None

    async def __call__(self, scope, receive, send):
        if self._app is None:
            self._app = create_app()
        await self._app(scope, receive, send)


app = _LazyApp()
