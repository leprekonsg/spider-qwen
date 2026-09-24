"""Durable local run lifecycle and bounded in-process execution."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .profiles import PIPELINE_VERSION, OperatorProfile, get_profile
from ..observability.tracing import TraceEvent
from ..requirements import ProcurementRequest


TERMINAL_STATUSES = {"completed", "failed", "cancelled", "timed_out", "interrupted"}
ACTIVE_STATUSES = {"queued", "running", "cancelling"}
_SAFE_TRACE_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_TRACE_STEPS = {
    "search", "query_expand", "crag_evaluate", "crag_corrective", "geo_fallback",
    "verification_replan", "compiler_execute", "frontier_drain", "frontier_score",
    "frontier_rescore", "reasoning_trajectory", "fetch", "fetch_fallback", "wayback_recover",
    "page_judge", "page_role_gate", "qwen_json_extract", "pricing_subject_gate",
    "verify_claims", "emission_gate_input", "conformal_gate", "memory_credit_verify",
    "supplier_consolidation",
    "memory_recall",
    "retrieval_recipe_shadow",
}
_TRACE_TOOLS = {
    "mcp_search", "mock", "tinyfish_search", "tinyfish_fetch", "page_judge",
    "qwen_web_extractor", "wayback_cdx", "qwen_corrective", "search", "llm_compiler",
    "frontier", "qwen_frontier_scorer", "semantic_memory", "url_heuristic",
    "qwen_json_extractor", "minicheck_verifier", "conformal_abstainer", "supplier_identity",
    "query_rewrite",
    "retrieval_recipe",
}
_TRACE_STATUSES = {"success", "error", "blocked", "rejected"}


def _trace_phase(step: str) -> str:
    if step in {"search", "query_expand", "crag_evaluate", "crag_corrective", "geo_fallback",
                "verification_replan", "compiler_execute", "frontier_drain", "frontier_score",
                "frontier_rescore", "reasoning_trajectory"}:
        return "discovery"
    if step in {"fetch", "fetch_fallback", "wayback_recover", "page_judge", "page_role_gate",
                "retrieval_recipe_shadow"}:
        return "retrieval"
    if step in {"qwen_json_extract", "pricing_subject_gate"}:
        return "extraction"
    if step in {"verify_claims", "emission_gate_input", "conformal_gate", "memory_credit_verify"}:
        return "verification"
    if step == "supplier_consolidation":
        return "consolidation"
    if step == "memory_recall":
        return "memory"
    return "processing"


class RunServiceError(RuntimeError):
    """Base class for errors that API adapters can map consistently."""


class RunNotFound(RunServiceError):
    pass


class RunNotReady(RunServiceError):
    pass


class AdmissionRejected(RunServiceError):
    pass


class IdempotencyConflict(RunServiceError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _utc_now()).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def owner_state_dir(state_dir: str | Path, owner: str) -> Path:
    """Return the non-reversible filesystem scope for one validated owner."""
    validated = RunService._validate_owner(owner)
    digest = hashlib.sha256(validated.encode("utf-8")).hexdigest()
    return Path(state_dir) / "owners" / digest


def _acquire_process_lock(path: Path):
    handle = path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:  # pragma: no cover - exercised on POSIX CI
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError) as exc:
        handle.close()
        raise RuntimeError(
            f"Another RunService already owns '{path.parent}'. "
            "Use one server process per state directory."
        ) from exc
    return handle


def _release_process_lock(handle) -> None:
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:  # pragma: no cover - exercised on POSIX CI
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


class RunService:
    """A process-local scheduler backed by durable SQLite lifecycle records.

    SQLite preserves IDs, results, and events. Worker execution remains local to
    this process; startup marks work abandoned by a prior process as interrupted.
    """

    def __init__(
        self,
        *,
        state_dir: str | Path,
        controller_builder: Callable[..., Any],
        allow_live: bool = False,
        max_concurrency: int = 2,
        max_queued: int = 8,
        max_live_concurrency: int = 1,
        max_live_runs_per_utc_day: int = 20,
        max_deadline_seconds: int = 900,
    ) -> None:
        if (max_concurrency < 1 or max_queued < 0 or max_live_concurrency < 1
                or max_live_runs_per_utc_day < 1 or max_deadline_seconds < 1):
            raise ValueError(
                "Run limits must be positive (max_queued may be zero)."
            )
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.state_dir / "runs.sqlite3"
        self.controller_builder = controller_builder
        self.allow_live = bool(allow_live)
        self.max_concurrency = int(max_concurrency)
        self.max_queued = int(max_queued)
        self.max_live_concurrency = int(max_live_concurrency)
        self.max_live_runs_per_utc_day = int(max_live_runs_per_utc_day)
        self.max_deadline_seconds = int(max_deadline_seconds)
        self._lock = threading.RLock()
        self._futures: dict[str, Future] = {}
        self._loops: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Task]] = {}
        self._closed = False
        self._process_lock = _acquire_process_lock(self.state_dir / "runs.lock")
        try:
            self._init_db()
            self._interrupt_abandoned_runs()
            self._executor = ThreadPoolExecutor(
                max_workers=self.max_concurrency, thread_name_prefix="spider-qwen-run"
            )
        except BaseException:
            _release_process_lock(self._process_lock)
            raise

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    idempotency_key TEXT,
                    request_fingerprint TEXT NOT NULL,
                    query TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    country TEXT,
                    profile TEXT NOT NULL,
                    high_risk INTEGER NOT NULL DEFAULT 0,
                    serendipity INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    deadline_at TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    effective_config_json TEXT NOT NULL,
                    pipeline_version TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT,
                    UNIQUE(owner, idempotency_key)
                );
                CREATE INDEX IF NOT EXISTS runs_owner_created
                    ON runs(owner, created_at DESC);
                CREATE INDEX IF NOT EXISTS runs_status ON runs(status);
                CREATE TABLE IF NOT EXISTS run_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    detail_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS run_events_run_id
                    ON run_events(run_id, event_id);
                """
            )

            columns = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
            if "procurement_request_json" not in columns:
                conn.execute("ALTER TABLE runs ADD COLUMN procurement_request_json TEXT")

    def _interrupt_abandoned_runs(self) -> None:
        now = _iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT run_id FROM runs WHERE status IN ('queued','running','cancelling')"
            ).fetchall()
            for row in rows:
                conn.execute(
                    "UPDATE runs SET status='interrupted', updated_at=?, finished_at=?, "
                    "error=? WHERE run_id=?",
                    (now, now, "Service restarted before the worker completed.", row["run_id"]),
                )
                self._insert_event(conn, row["run_id"], "interrupted", {
                    "reason": "service_restart",
                    "message": "The local worker did not survive the service restart.",
                })
            conn.commit()

    @staticmethod
    def _insert_event(conn: sqlite3.Connection, run_id: str, kind: str, detail: dict) -> None:
        conn.execute(
            "INSERT INTO run_events(run_id,created_at,kind,detail_json) VALUES(?,?,?,?)",
            (run_id, _iso(), kind, _json(detail)),
        )

    def _builder_accepts_trace_callback(self) -> bool:
        """Keep injected controller builders compatible with the old contract."""
        try:
            parameters = inspect.signature(self.controller_builder).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(
            parameter.name == "trace_callback"
            or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )

    @staticmethod
    def _safe_trace_identifier(value: object, *, fallback: str) -> str:
        text = str(value or "")
        return text if _SAFE_TRACE_IDENTIFIER.fullmatch(text) else fallback

    @staticmethod
    def _bounded_trace_count(value: object, *, maximum: int = 1_000_000) -> int:
        try:
            return max(0, min(int(value), maximum))
        except (TypeError, ValueError):
            return 0

    def _safe_trace_detail(self, event: TraceEvent) -> dict[str, Any]:
        """Make a durable progress event from trace metadata, never trace payloads."""
        raw_step = self._safe_trace_identifier(event.step, fallback="custom")
        raw_tool = self._safe_trace_identifier(event.tool, fallback="custom")
        raw_status = self._safe_trace_identifier(event.status, fallback="unknown")
        step = raw_step if raw_step in _TRACE_STEPS else "custom"
        status = raw_status if raw_status in _TRACE_STATUSES else "unknown"
        phase = _trace_phase(step)
        detail: dict[str, Any] = {
            "phase": phase,
            "step": step,
            "tool": raw_tool if raw_tool in _TRACE_TOOLS else "custom",
            "status": status,
            "input_count": self._bounded_trace_count(event.input_count),
            "output_count": self._bounded_trace_count(event.output_count),
            "latency_ms": self._bounded_trace_count(event.latency_ms, maximum=3_600_000),
            "message": f"{phase}: {step.replace('_', ' ')} ({status})",
        }
        source = event.detail if isinstance(event.detail, dict) else {}
        counters = {
            key: self._bounded_trace_count(source[key])
            for key in ("cache_hits", "merged_candidates", "pages")
            if isinstance(source.get(key), int) and not isinstance(source.get(key), bool)
        }
        if counters:
            detail["counters"] = counters
        return detail

    def _record_trace_event(self, run_id: str, event: TraceEvent) -> None:
        """Append one trace event while its run is active.

        A write failure deliberately propagates through ``Tracer.record`` and
        fails the run. Skipping a callback after a terminal transition is the
        only intentional omission: terminal event ordering is immutable.
        """
        try:
            detail = self._safe_trace_detail(event)
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
                if row is None or row["status"] in TERMINAL_STATUSES:
                    conn.rollback()
                    return
                self._insert_event(conn, run_id, "trace", detail)
                conn.commit()
        except Exception as exc:
            raise RunServiceError("Unable to persist run progress event.") from exc

    @staticmethod
    def _validate_owner(owner: str) -> str:
        owner = owner.strip()
        if not owner or len(owner) > 128:
            raise ValueError("Owner must contain 1 to 128 characters.")
        return owner

    def start(
        self,
        request: dict[str, Any],
        *,
        owner: str,
        idempotency_key: str | None = None,
    ) -> dict:
        if self._closed:
            raise RunServiceError("Run service is closed.")
        owner = self._validate_owner(owner)
        profile = get_profile(str(request.get("profile") or "offline_demo"))
        if profile.live and not self.allow_live:
            raise PermissionError(
                "Live profiles are disabled. Set SPIDER_QWEN_ALLOW_LIVE=1 on the server to enable them."
            )
        query = str(request.get("query") or "").strip()
        if not query:
            raise ValueError("Query must not be empty.")
        procurement = ProcurementRequest(
            query=query, requirements=request.get("requirements") or [],
            requirements_confirmed=request.get("requirements_confirmed", False),
            supplier_sources=request.get("supplier_sources") or {},
        )
        mode = str(request.get("mode") or "auto")
        country = request.get("country")
        deadline_seconds = int(request.get("deadline_seconds") or profile.default_deadline_seconds)
        if deadline_seconds < 1 or deadline_seconds > self.max_deadline_seconds:
            raise ValueError(
                f"deadline_seconds must be between 1 and {self.max_deadline_seconds}."
            )
        idempotency_key = idempotency_key.strip() if idempotency_key else None
        if idempotency_key is not None and (not idempotency_key or len(idempotency_key) > 200):
            raise ValueError("Idempotency key must contain 1 to 200 characters.")

        normalized = {
            "query": query,
            "procurement_request": procurement.model_dump(mode="json"),
            "mode": mode,
            "country": country,
            "profile": profile.name,
            "deadline_seconds": deadline_seconds,
            "high_risk": bool(request.get("high_risk", False)),
            "serendipity": bool(request.get("serendipity", False)),
        }
        fingerprint = hashlib.sha256(_json(normalized).encode("utf-8")).hexdigest()
        now = _utc_now()
        run_id = f"run_{uuid.uuid4().hex}"
        effective = profile.effective_config(deadline_seconds=deadline_seconds)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                existing = conn.execute(
                    "SELECT * FROM runs WHERE owner=? AND idempotency_key=?",
                    (owner, idempotency_key),
                ).fetchone()
                if existing:
                    conn.rollback()
                    if existing["request_fingerprint"] != fingerprint:
                        raise IdempotencyConflict(
                            "That idempotency key was already used for a different request."
                        )
                    return self._status_from_row(existing)
            active = conn.execute(
                "SELECT COUNT(*) FROM runs WHERE status IN ('queued','running','cancelling')"
            ).fetchone()[0]
            if active >= self.max_concurrency + self.max_queued:
                conn.rollback()
                raise AdmissionRejected("Run capacity is full; retry after an active run finishes.")
            if profile.live:
                live_active = conn.execute(
                    "SELECT COUNT(*) FROM runs WHERE status IN ('queued','running','cancelling') "
                    "AND profile != 'offline_demo'"
                ).fetchone()[0]
                if live_active >= self.max_live_concurrency:
                    conn.rollback()
                    raise AdmissionRejected(
                        "Live run capacity is full; retry after the active live run finishes."
                    )
                utc_day = now.date().isoformat()
                live_today = conn.execute(
                    "SELECT COUNT(*) FROM runs WHERE profile != 'offline_demo' "
                    "AND substr(created_at,1,10)=?",
                    (utc_day,),
                ).fetchone()[0]
                if live_today >= self.max_live_runs_per_utc_day:
                    conn.rollback()
                    raise AdmissionRejected(
                        "Daily live-run admission cap reached; retry after 00:00 UTC or raise "
                        "SPIDER_QWEN_MAX_LIVE_RUNS_PER_UTC_DAY."
                    )
            deadline_at = now + timedelta(seconds=deadline_seconds)
            conn.execute(
                """INSERT INTO runs(
                    run_id,owner,idempotency_key,request_fingerprint,query,mode,country,
                    profile,high_risk,serendipity,status,created_at,updated_at,deadline_at,
                    effective_config_json,pipeline_version
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, owner, idempotency_key, fingerprint, query, mode, country,
                    profile.name, int(normalized["high_risk"]), int(normalized["serendipity"]),
                    "queued", _iso(now), _iso(now), _iso(deadline_at), _json(effective),
                    PIPELINE_VERSION,
                ),
            )
            self._insert_event(conn, run_id, "queued", {"profile": profile.name})
            conn.execute("UPDATE runs SET procurement_request_json=? WHERE run_id=?",
                         (_json(procurement.model_dump(mode="json")), run_id))
            conn.commit()

        with self._lock:
            if self._closed:
                with self._connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    self._finish(conn, run_id, "cancelled", error="Service stopped before execution started.")
                    self._insert_event(conn, run_id, "cancellation_acknowledged", {"phase": "shutdown"})
                    conn.commit()
                raise RunServiceError("Run service is closed.")
            future = self._executor.submit(self._worker, run_id, profile)
            self._futures[run_id] = future
            future.add_done_callback(lambda _future, rid=run_id: self._discard_future(rid))
        return self.status(run_id, owner=owner)

    def _discard_future(self, run_id: str) -> None:
        with self._lock:
            self._futures.pop(run_id, None)
            self._loops.pop(run_id, None)

    def _worker(self, run_id: str, profile: OperatorProfile) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None or row["status"] not in {"queued", "cancelling"}:
                conn.rollback()
                return
            if row["cancel_requested"]:
                self._finish(conn, run_id, "cancelled", error="Cancelled before execution started.")
                self._insert_event(conn, run_id, "cancellation_acknowledged", {"phase": "queued"})
                conn.commit()
                return
            conn.execute(
                "UPDATE runs SET status='running', started_at=?, updated_at=? WHERE run_id=?",
                (_iso(), _iso(), run_id),
            )
            self._insert_event(conn, run_id, "started", {"profile": profile.name})
            conn.commit()

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        task: asyncio.Task | None = None
        row = self._row_for_worker(run_id)
        effective_config = json.loads(row["effective_config_json"])
        configured_seconds = int(effective_config["deadline_seconds"])
        trace_failures: list[RunServiceError] = []

        def record_trace(event: TraceEvent, *, rid: str = run_id) -> None:
            if trace_failures:
                raise trace_failures[0]
            try:
                self._record_trace_event(rid, event)
            except RunServiceError as exc:
                # Some optional controller seams catch Exception and continue.
                # Keep this failure sticky so a run cannot complete with a lost
                # durable trace event even when the immediate exception is caught.
                trace_failures.append(exc)
                raise

        try:
            scoped_state_dir = owner_state_dir(self.state_dir, row["owner"])
            builder_config = {
                "offline": profile.offline,
                "state_dir": str(scoped_state_dir),
                "qwen_json": profile.qwen_json,
                "verify": profile.verify,
                "require_review": profile.require_review,
                "rfq_grade_floor": profile.rfq_grade_floor,
                "expected_config_fingerprint": effective_config["config_fingerprint"],
            }
            if self._builder_accepts_trace_callback():
                builder_config["trace_callback"] = record_trace
            controller = self.controller_builder(**builder_config)
            row = self._row_for_worker(run_id)
            deadline = datetime.fromisoformat(row["deadline_at"].replace("Z", "+00:00"))
            remaining = (deadline - _utc_now()).total_seconds()
            if remaining <= 0:
                self._timed_out(run_id, configured_seconds)
                return
            procurement = json.loads(row["procurement_request_json"] or "{}")
            requirement_options = {}
            if procurement.get("requirements"):
                requirement_options = {
                    "requirements": procurement["requirements"],
                    "requirements_confirmed": procurement.get("requirements_confirmed", False),
                    "supplier_sources": procurement.get("supplier_sources", {}),
                }
            coro = controller.run(
                row["query"],
                mode=row["mode"],
                target_country=row["country"],
                high_risk=bool(row["high_risk"]),
                serendipity=bool(row["serendipity"]),
                run_id=run_id,
                **requirement_options,
            )
            task = loop.create_task(coro)
            with self._lock:
                self._loops[run_id] = (loop, task)
            latest = self._row_for_worker(run_id)
            if latest["cancel_requested"] or self._closed:
                task.cancel()
            result = loop.run_until_complete(asyncio.wait_for(task, timeout=remaining))
            if trace_failures:
                raise trace_failures[0]
            payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
            self._complete(run_id, payload)
        except asyncio.CancelledError:
            self._cancelled(run_id)
        except TimeoutError:
            self._timed_out(run_id, configured_seconds)
        except Exception as exc:
            self._failed(run_id, exc)
        finally:
            with self._lock:
                self._loops.pop(run_id, None)
            if task is not None and not task.done():
                task.cancel()
                try:
                    loop.run_until_complete(task)
                except BaseException:
                    pass
            loop.close()

    def _row_for_worker(self, run_id: str) -> sqlite3.Row:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise RunNotFound(run_id)
        return row

    @staticmethod
    def _finish(
        conn: sqlite3.Connection,
        run_id: str,
        status: str,
        *,
        result_json: str | None = None,
        error: str | None = None,
    ) -> None:
        now = _iso()
        conn.execute(
            "UPDATE runs SET status=?,result_json=?,error=?,updated_at=?,finished_at=? WHERE run_id=?",
            (status, result_json, error, now, now, run_id),
        )

    def _complete(self, run_id: str, payload: Any) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT profile,effective_config_json,pipeline_version,cancel_requested,deadline_at "
                "FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is not None and row["cancel_requested"]:
                self._finish(conn, run_id, "cancelled", error="Cancellation acknowledged by worker.")
                self._insert_event(conn, run_id, "cancellation_acknowledged", {"phase": "completion"})
                conn.commit()
                return
            deadline = datetime.fromisoformat(row["deadline_at"].replace("Z", "+00:00"))
            if _utc_now() >= deadline:
                seconds = json.loads(row["effective_config_json"])["deadline_seconds"]
                self._finish(conn, run_id, "timed_out", error=f"Run exceeded its {seconds}s deadline.")
                self._insert_event(conn, run_id, "timed_out", {"deadline_seconds": seconds})
                conn.commit()
                return
            if isinstance(payload, dict) and row is not None:
                payload["profile"] = row["profile"]
                payload["effective_config"] = json.loads(row["effective_config_json"])
                payload["pipeline_version"] = row["pipeline_version"]
                payload["execution"] = {
                    "service": "local",
                    "durable": True,
                    "distributed_scheduler": False,
                }
            self._finish(conn, run_id, "completed", result_json=_json(payload))
            self._insert_event(conn, run_id, "completed", {})
            conn.commit()

    def _cancelled(self, run_id: str) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._finish(conn, run_id, "cancelled", error="Cancellation acknowledged by worker.")
            self._insert_event(conn, run_id, "cancellation_acknowledged", {"phase": "running"})
            conn.commit()

    def _timed_out(self, run_id: str, seconds: int) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._finish(conn, run_id, "timed_out", error=f"Run exceeded its {seconds}s deadline.")
            self._insert_event(conn, run_id, "timed_out", {"deadline_seconds": seconds})
            conn.commit()

    def _failed(self, run_id: str, exc: BaseException) -> None:
        message = f"{type(exc).__name__}: {exc}"
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._finish(conn, run_id, "failed", error=message[:2000])
            self._insert_event(conn, run_id, "failed", {"error": message[:500]})
            conn.commit()

    def _owned_row(self, run_id: str, owner: str) -> sqlite3.Row:
        owner = self._validate_owner(owner)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id=? AND owner=?", (run_id, owner)
            ).fetchone()
        if row is None:
            raise RunNotFound(f"Run '{run_id}' was not found.")
        return row

    @staticmethod
    def _status_from_row(row: sqlite3.Row) -> dict:
        return {
            "run_id": row["run_id"],
            "procurement_request": json.loads(row["procurement_request_json"] or "{}"),
            "status": row["status"],
            "profile": row["profile"],
            "query": row["query"],
            "mode": row["mode"],
            "country": row["country"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "deadline_at": row["deadline_at"],
            "cancel_requested": bool(row["cancel_requested"]),
            "effective_config": json.loads(row["effective_config_json"]),
            "pipeline_version": row["pipeline_version"],
            "error": row["error"],
        }

    def status(self, run_id: str, *, owner: str) -> dict:
        return self._status_from_row(self._owned_row(run_id, owner))

    def result(self, run_id: str, *, owner: str) -> dict:
        row = self._owned_row(run_id, owner)
        if row["status"] != "completed":
            raise RunNotReady(
                f"Run '{run_id}' has status '{row['status']}'; a result exists only when completed."
            )
        return json.loads(row["result_json"])

    def events(self, run_id: str, *, owner: str, after_id: int = 0, limit: int = 100) -> list[dict]:
        self._owned_row(run_id, owner)
        limit = max(1, min(int(limit), 500))
        after_id = max(0, int(after_id))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT event_id,created_at,kind,detail_json FROM run_events "
                "WHERE run_id=? AND event_id>? ORDER BY event_id LIMIT ?",
                (run_id, after_id, limit),
            ).fetchall()
        events = []
        for row in rows:
            detail = json.loads(row["detail_json"])
            events.append({
                "id": row["event_id"],
                "event_id": row["event_id"],
                "run_id": run_id,
                "created_at": row["created_at"],
                "kind": row["kind"],
                "phase": detail.get("phase"),
                "detail": detail,
                "message": detail.get("message", row["kind"]),
            })
        return events

    def cancel(self, run_id: str, *, owner: str) -> dict:
        row = self._owned_row(run_id, owner)
        if row["status"] in TERMINAL_STATUSES:
            return self._status_from_row(row)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if current["status"] in TERMINAL_STATUSES:
                conn.rollback()
                return self.status(run_id, owner=owner)
            conn.execute(
                "UPDATE runs SET cancel_requested=1,status='cancelling',updated_at=? WHERE run_id=?",
                (_iso(), run_id),
            )
            self._insert_event(conn, run_id, "cancellation_requested", {})
            conn.commit()
        with self._lock:
            future = self._futures.get(run_id)
            running = self._loops.get(run_id)
            if running:
                loop, task = running
                loop.call_soon_threadsafe(task.cancel)
            elif future is not None and future.cancel():
                with self._connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    self._finish(conn, run_id, "cancelled", error="Cancelled before execution started.")
                    self._insert_event(conn, run_id, "cancellation_acknowledged", {"phase": "queued"})
                    conn.commit()
        return self.status(run_id, owner=owner)

    def wait(self, run_id: str, *, owner: str, timeout: float | None = None) -> dict:
        started = time.monotonic()
        while True:
            status = self.status(run_id, owner=owner)
            if status["status"] in TERMINAL_STATUSES:
                return status
            if timeout is not None and time.monotonic() - started >= timeout:
                raise TimeoutError(f"Timed out waiting for run '{run_id}'.")
            time.sleep(0.01)

    def close(self, *, wait: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                rows = conn.execute(
                    "SELECT run_id,cancel_requested FROM runs "
                    "WHERE status IN ('queued','running','cancelling')"
                ).fetchall()
                now = _iso()
                for row in rows:
                    conn.execute(
                        "UPDATE runs SET cancel_requested=1,status='cancelling',updated_at=? "
                        "WHERE run_id=?",
                        (now, row["run_id"]),
                    )
                    if not row["cancel_requested"]:
                        self._insert_event(conn, row["run_id"], "cancellation_requested", {
                            "reason": "service_shutdown",
                        })
                conn.commit()
            active = list(self._loops.values())
            queued = list(self._futures.items())
        for loop, task in active:
            loop.call_soon_threadsafe(task.cancel)
        for run_id, future in queued:
            if future.cancel():
                with self._connect() as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    self._finish(conn, run_id, "cancelled", error="Service stopped before execution started.")
                    self._insert_event(conn, run_id, "cancellation_acknowledged", {"phase": "shutdown"})
                    conn.commit()
        self._executor.shutdown(wait=wait, cancel_futures=True)
        if wait:
            _release_process_lock(self._process_lock)


def load_run_result(state_dir: str | Path, run_id: str, *, owner: str) -> dict:
    """Read one completed owner-scoped result without starting or recovering workers."""
    db_path = Path(state_dir) / "runs.sqlite3"
    if not db_path.is_file():
        raise RunNotFound(f"Run '{run_id}' was not found.")
    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT status,result_json FROM runs WHERE run_id=? AND owner=?",
            (run_id, RunService._validate_owner(owner)),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise RunNotFound(f"Run '{run_id}' was not found.")
    if row["status"] != "completed":
        raise RunNotReady(
            f"Run '{run_id}' has status '{row['status']}'; a result exists only when completed."
        )
    return json.loads(row["result_json"])
