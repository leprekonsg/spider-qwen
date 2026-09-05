"""Checkpointed, quota-bound research for already identified entities."""

from __future__ import annotations

import inspect
import json
import os
import re
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..evidence.models import EvidenceRef

_T = TypeVar("_T")
_IDENTIFIER = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")


class EntityResearchError(RuntimeError):
    """Base class for bounded entity-research failures."""


class DatasetNotFound(EntityResearchError):
    pass


class DatasetConflict(EntityResearchError):
    pass


class ResourceLimitExceeded(EntityResearchError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _identifier(value: str, field: str, *, allow_empty: bool = False) -> str:
    text = str(value or "").strip()
    if allow_empty and not text:
        return ""
    if not _IDENTIFIER.fullmatch(text):
        raise ValueError(f"{field} must contain 1 to 200 printable characters")
    return text


def _acquire_process_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
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
            f"Another EntityResearchRunner already owns '{path.parent}'."
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


class EntityResearchLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_provider_calls: int = Field(ge=0, le=1_000_000)
    max_cost_micros: int = Field(ge=0, le=10**15)


class EntityResearchTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    entity_id: str
    offering_id: str = ""
    requirement_id: str
    input_payload: dict[str, Any] = Field(default_factory=dict)
    max_attempts: int = Field(default=3, ge=1, le=20)

    @model_validator(mode="after")
    def validate_identifiers(self):
        object.__setattr__(self, "entity_id", _identifier(self.entity_id, "entity_id"))
        object.__setattr__(
            self, "offering_id", _identifier(self.offering_id, "offering_id", allow_empty=True)
        )
        object.__setattr__(
            self, "requirement_id", _identifier(self.requirement_id, "requirement_id")
        )
        if len(_json(self.input_payload).encode("utf-8")) > 64 * 1024:
            raise ValueError("input_payload must be at most 64 KiB when serialized")
        return self


class EntityResearchClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: str
    offering_id: str = ""
    requirement_id: str
    status: Literal["supported", "contradicted", "not_found"]
    value: str | None = Field(default=None, max_length=10_000)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=100)
    observed_at: str | None = None
    reason: str = Field(default="", max_length=2_000)
    excerpts: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_claim(self):
        self.entity_id = _identifier(self.entity_id, "entity_id")
        self.offering_id = _identifier(
            self.offering_id, "offering_id", allow_empty=True
        )
        self.requirement_id = _identifier(self.requirement_id, "requirement_id")
        if self.status != "not_found" and not self.evidence_refs:
            raise ValueError(
                f"{self.status} entity-research claims require at least one evidence_ref"
            )
        if any(len(excerpt) > 4_000 for excerpt in self.excerpts):
            raise ValueError("Entity-research claim excerpts must be at most 4000 characters")
        return self


class EntityWorkItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    work_id: str
    owner: str
    dataset_id: str
    input_version: str
    entity_id: str
    offering_id: str = ""
    requirement_id: str
    input_payload: dict[str, Any] = Field(default_factory=dict)
    status: Literal["queued", "running", "completed", "failed"]
    attempts: int
    max_attempts: int
    last_error: str | None = None


class EntityWorkContext:
    """Identity-locked resource gateway passed to one acquisition handler."""

    def __init__(self, runner: "EntityResearchRunner", item: EntityWorkItem) -> None:
        self._runner = runner
        self._item = item

    @property
    def owner(self) -> str:
        return self._item.owner

    @property
    def dataset_id(self) -> str:
        return self._item.dataset_id

    @property
    def input_version(self) -> str:
        return self._item.input_version

    @property
    def work_id(self) -> str:
        return self._item.work_id

    def acquire(
        self,
        operation: Callable[[], _T],
        *,
        provider: str,
        provider_calls: int = 1,
        max_cost_micros: int,
    ) -> _T:
        """Reserve an upper bound atomically, then invoke one synchronous operation."""

        self._runner._reserve(
            self._item,
            provider=provider,
            provider_calls=provider_calls,
            max_cost_micros=max_cost_micros,
        )
        result = operation()
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            raise TypeError("acquire operation returned an awaitable; use acquire_async")
        return result

    async def acquire_async(
        self,
        operation: Callable[[], Awaitable[_T]],
        *,
        provider: str,
        provider_calls: int = 1,
        max_cost_micros: int,
    ) -> _T:
        """Reserve an upper bound atomically, then invoke one asynchronous operation."""

        self._runner._reserve(
            self._item,
            provider=provider,
            provider_calls=provider_calls,
            max_cost_micros=max_cost_micros,
        )
        result = operation()
        if not inspect.isawaitable(result):
            raise TypeError("acquire_async operation must return an awaitable")
        return await result


class EntityResearchRunner:
    """Run bounded per-entity work without launching a full controller per item.

    Provider-call and monetary limits cover only operations invoked through the
    supplied ``EntityWorkContext``. They are not global provider-spend controls.
    """

    def __init__(
        self,
        state_dir: str | Path,
        handler: Callable[[EntityWorkItem, EntityWorkContext], EntityResearchClaim | dict],
        *,
        max_concurrency: int = 4,
    ) -> None:
        if max_concurrency < 1 or max_concurrency > 64:
            raise ValueError("max_concurrency must be between 1 and 64")
        if not callable(handler):
            raise TypeError("handler must be callable")
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.state_dir / "entity_research.sqlite3"
        self.handler = handler
        self.max_concurrency = int(max_concurrency)
        self._run_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._closing = False
        self._closed = False
        self._process_lock = _acquire_process_lock(
            self.state_dir / "entity_research.lock"
        )
        try:
            self._init_db()
            self._recover_running()
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
                CREATE TABLE IF NOT EXISTS entity_datasets (
                    owner TEXT NOT NULL,
                    dataset_id TEXT NOT NULL,
                    input_version TEXT NOT NULL,
                    max_provider_calls INTEGER NOT NULL,
                    max_cost_micros INTEGER NOT NULL,
                    provider_calls_reserved INTEGER NOT NULL DEFAULT 0,
                    cost_micros_reserved INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(owner,dataset_id,input_version)
                );
                CREATE TABLE IF NOT EXISTS entity_work_items (
                    work_id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    dataset_id TEXT NOT NULL,
                    input_version TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    offering_id TEXT NOT NULL DEFAULT '',
                    requirement_id TEXT NOT NULL,
                    input_fingerprint TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL,
                    claim_json TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    UNIQUE(owner,dataset_id,entity_id,offering_id,requirement_id,input_version),
                    FOREIGN KEY(owner,dataset_id,input_version)
                      REFERENCES entity_datasets(owner,dataset_id,input_version)
                );
                CREATE INDEX IF NOT EXISTS entity_work_scope_status
                  ON entity_work_items(owner,dataset_id,input_version,status,work_id);
                CREATE TABLE IF NOT EXISTS entity_resource_reservations (
                    reservation_id TEXT PRIMARY KEY,
                    work_id TEXT NOT NULL REFERENCES entity_work_items(work_id),
                    provider TEXT NOT NULL,
                    provider_calls INTEGER NOT NULL,
                    max_cost_micros INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS entity_reservations_work
                  ON entity_resource_reservations(work_id,created_at);
                """
            )

    def _recover_running(self) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now = _utc_now()
            conn.execute(
                "UPDATE entity_work_items SET status='queued',started_at=NULL,updated_at=?,"
                "last_error=? WHERE status='running' AND attempts < max_attempts",
                (now, "Interrupted by a prior process; queued for a targeted retry."),
            )
            conn.execute(
                "UPDATE entity_work_items SET status='failed',finished_at=?,updated_at=?,"
                "last_error=? WHERE status='running' AND attempts >= max_attempts",
                (now, now, "Interrupted by a prior process after the final allowed attempt."),
            )
            conn.commit()

    @staticmethod
    def _scope(owner: str, dataset_id: str, input_version: str) -> tuple[str, str, str]:
        return (
            _identifier(owner, "owner"),
            _identifier(dataset_id, "dataset_id"),
            _identifier(input_version, "input_version"),
        )

    @staticmethod
    def _work_id(
        owner: str,
        dataset_id: str,
        input_version: str,
        task: EntityResearchTask,
    ) -> str:
        import hashlib

        key = _json([
            owner,
            dataset_id,
            task.entity_id,
            task.offering_id,
            task.requirement_id,
            input_version,
        ])
        return "ework_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]

    @staticmethod
    def _input_fingerprint(task: EntityResearchTask) -> str:
        import hashlib

        return hashlib.sha256(
            _json(task.input_payload).encode("utf-8")
        ).hexdigest()

    def submit(
        self,
        *,
        owner: str,
        dataset_id: str,
        input_version: str,
        tasks: list[EntityResearchTask | dict],
        limits: EntityResearchLimits | dict,
    ) -> dict[str, Any]:
        """Create an idempotent dataset and its immutable keyed work items."""

        self._ensure_open()
        owner, dataset_id, input_version = self._scope(owner, dataset_id, input_version)
        parsed_tasks = [
            task if isinstance(task, EntityResearchTask) else EntityResearchTask.model_validate(task)
            for task in tasks
        ]
        if not parsed_tasks:
            raise ValueError("tasks must contain at least one entity research task")
        if len(parsed_tasks) > 10_000:
            raise ValueError("tasks must contain at most 10000 work items")
        parsed_limits = (
            limits if isinstance(limits, EntityResearchLimits)
            else EntityResearchLimits.model_validate(limits)
        )
        now = _utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            dataset = conn.execute(
                "SELECT * FROM entity_datasets WHERE owner=? AND dataset_id=? AND input_version=?",
                (owner, dataset_id, input_version),
            ).fetchone()
            if dataset is None:
                conn.execute(
                    "INSERT INTO entity_datasets(owner,dataset_id,input_version,max_provider_calls,"
                    "max_cost_micros,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        owner,
                        dataset_id,
                        input_version,
                        parsed_limits.max_provider_calls,
                        parsed_limits.max_cost_micros,
                        now,
                        now,
                    ),
                )
            elif (
                dataset["max_provider_calls"] != parsed_limits.max_provider_calls
                or dataset["max_cost_micros"] != parsed_limits.max_cost_micros
            ):
                conn.rollback()
                raise DatasetConflict(
                    "Dataset limits are immutable for an input version; submit a new input_version."
                )
            seen: set[str] = set()
            for task in parsed_tasks:
                work_id = self._work_id(owner, dataset_id, input_version, task)
                if work_id in seen:
                    conn.rollback()
                    raise ValueError(f"Duplicate entity research task {work_id!r}")
                seen.add(work_id)
                fingerprint = self._input_fingerprint(task)
                existing = conn.execute(
                    "SELECT input_fingerprint,max_attempts FROM entity_work_items WHERE work_id=?",
                    (work_id,),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["input_fingerprint"] != fingerprint
                        or existing["max_attempts"] != task.max_attempts
                    ):
                        conn.rollback()
                        raise DatasetConflict(
                            f"Work item {work_id!r} changed without a new input_version."
                        )
                    continue
                conn.execute(
                    "INSERT INTO entity_work_items(work_id,owner,dataset_id,input_version,entity_id,"
                    "offering_id,requirement_id,input_fingerprint,input_json,status,max_attempts,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'queued',?,?,?)",
                    (
                        work_id,
                        owner,
                        dataset_id,
                        input_version,
                        task.entity_id,
                        task.offering_id,
                        task.requirement_id,
                        fingerprint,
                        _json(task.input_payload),
                        task.max_attempts,
                        now,
                        now,
                    ),
                )
            conn.execute(
                "UPDATE entity_datasets SET updated_at=? WHERE owner=? AND dataset_id=? "
                "AND input_version=?",
                (now, owner, dataset_id, input_version),
            )
            conn.commit()
        return self._read_status(owner, dataset_id, input_version)

    def _dataset_row(
        self, conn: sqlite3.Connection, owner: str, dataset_id: str, input_version: str
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM entity_datasets WHERE owner=? AND dataset_id=? AND input_version=?",
            (owner, dataset_id, input_version),
        ).fetchone()
        if row is None:
            raise DatasetNotFound(
                f"Entity research dataset {dataset_id!r} input {input_version!r} was not found."
            )
        return row

    @staticmethod
    def _item_from_row(row: sqlite3.Row) -> EntityWorkItem:
        return EntityWorkItem(
            work_id=row["work_id"],
            owner=row["owner"],
            dataset_id=row["dataset_id"],
            input_version=row["input_version"],
            entity_id=row["entity_id"],
            offering_id=row["offering_id"],
            requirement_id=row["requirement_id"],
            input_payload=json.loads(row["input_json"]),
            status=row["status"],
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            last_error=row["last_error"],
        )

    def _claim_next(
        self, owner: str, dataset_id: str, input_version: str
    ) -> EntityWorkItem | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._dataset_row(conn, owner, dataset_id, input_version)
            row = conn.execute(
                "SELECT * FROM entity_work_items WHERE owner=? AND dataset_id=? AND input_version=? "
                "AND status='queued' AND attempts < max_attempts ORDER BY work_id LIMIT 1",
                (owner, dataset_id, input_version),
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            now = _utc_now()
            changed = conn.execute(
                "UPDATE entity_work_items SET status='running',attempts=attempts+1,"
                "started_at=?,finished_at=NULL,last_error=NULL,updated_at=? "
                "WHERE work_id=? AND status='queued'",
                (now, now, row["work_id"]),
            ).rowcount
            if changed != 1:
                conn.rollback()
                return None
            claimed = conn.execute(
                "SELECT * FROM entity_work_items WHERE work_id=?", (row["work_id"],)
            ).fetchone()
            conn.commit()
        return self._item_from_row(claimed)

    def _reserve(
        self,
        item: EntityWorkItem,
        *,
        provider: str,
        provider_calls: int,
        max_cost_micros: int,
    ) -> str:
        provider = _identifier(provider, "provider")
        if type(provider_calls) is not int or provider_calls < 1:
            raise ValueError("provider_calls must be a positive integer")
        if type(max_cost_micros) is not int or max_cost_micros < 0:
            raise ValueError("max_cost_micros must be a non-negative integer")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT status,attempts,owner,dataset_id,input_version FROM entity_work_items "
                "WHERE work_id=?",
                (item.work_id,),
            ).fetchone()
            if (
                current is None
                or current["status"] != "running"
                or current["attempts"] != item.attempts
            ):
                conn.rollback()
                raise EntityResearchError(
                    f"Work item {item.work_id!r} is not running; resource reservation denied."
                )
            if (
                current["owner"] != item.owner
                or current["dataset_id"] != item.dataset_id
                or current["input_version"] != item.input_version
            ):
                conn.rollback()
                raise EntityResearchError("Work context identity no longer matches its persisted item.")
            dataset = self._dataset_row(
                conn, item.owner, item.dataset_id, item.input_version
            )
            next_calls = dataset["provider_calls_reserved"] + provider_calls
            next_cost = dataset["cost_micros_reserved"] + max_cost_micros
            if next_calls > dataset["max_provider_calls"]:
                conn.rollback()
                raise ResourceLimitExceeded(
                    f"Dataset provider-call reservation would exceed "
                    f"{dataset['max_provider_calls']} calls."
                )
            if next_cost > dataset["max_cost_micros"]:
                conn.rollback()
                raise ResourceLimitExceeded(
                    f"Dataset monetary reservation would exceed "
                    f"{dataset['max_cost_micros']} micro-units."
                )
            reservation_id = f"eres_{uuid.uuid4().hex}"
            now = _utc_now()
            conn.execute(
                "UPDATE entity_datasets SET provider_calls_reserved=?,cost_micros_reserved=?,"
                "updated_at=? WHERE owner=? AND dataset_id=? AND input_version=?",
                (
                    next_calls,
                    next_cost,
                    now,
                    item.owner,
                    item.dataset_id,
                    item.input_version,
                ),
            )
            conn.execute(
                "INSERT INTO entity_resource_reservations(reservation_id,work_id,provider,"
                "provider_calls,max_cost_micros,created_at) VALUES(?,?,?,?,?,?)",
                (
                    reservation_id,
                    item.work_id,
                    provider,
                    provider_calls,
                    max_cost_micros,
                    now,
                ),
            )
            conn.commit()
        return reservation_id

    def _complete(self, item: EntityWorkItem, raw_claim: EntityResearchClaim | dict) -> None:
        claim = (
            raw_claim
            if isinstance(raw_claim, EntityResearchClaim)
            else EntityResearchClaim.model_validate(raw_claim)
        )
        expected = (item.entity_id, item.offering_id, item.requirement_id)
        actual = (claim.entity_id, claim.offering_id, claim.requirement_id)
        if actual != expected:
            raise ValueError(
                f"Handler claim identity {actual!r} does not match locked work item {expected!r}."
            )
        now = _utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                "UPDATE entity_work_items SET status='completed',claim_json=?,last_error=NULL,"
                "finished_at=?,updated_at=? WHERE work_id=? AND status='running' AND attempts=?",
                (_json(claim.model_dump(mode="json")), now, now, item.work_id, item.attempts),
            ).rowcount
            if changed != 1:
                conn.rollback()
                raise EntityResearchError(
                    f"Work item {item.work_id!r} left running state before completion."
                )
            conn.commit()

    def _fail(self, item: EntityWorkItem, error: BaseException) -> None:
        message = str(error).strip() or error.__class__.__name__
        now = _utc_now()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE entity_work_items SET status='failed',last_error=?,finished_at=?,"
                "updated_at=? WHERE work_id=? AND status='running' AND attempts=?",
                (message[:2000], now, now, item.work_id, item.attempts),
            )
            conn.commit()

    def _worker(self, owner: str, dataset_id: str, input_version: str) -> None:
        while True:
            item = self._claim_next(owner, dataset_id, input_version)
            if item is None:
                return
            context = EntityWorkContext(self, item)
            try:
                claim = self.handler(item, context)
                if inspect.isawaitable(claim):
                    if inspect.iscoroutine(claim):
                        claim.close()
                    raise TypeError("Entity research handler must be synchronous")
                self._complete(item, claim)
            except Exception as exc:
                self._fail(item, exc)

    def run_pending(
        self, *, owner: str, dataset_id: str, input_version: str
    ) -> dict[str, Any]:
        """Execute queued work with the runner's bounded local concurrency."""

        owner, dataset_id, input_version = self._scope(owner, dataset_id, input_version)
        if not self._run_lock.acquire(blocking=False):
            raise EntityResearchError("This runner is already executing entity research work.")
        try:
            self._ensure_open()
            with self._connect() as conn:
                self._dataset_row(conn, owner, dataset_id, input_version)
                queued = conn.execute(
                    "SELECT COUNT(*) FROM entity_work_items WHERE owner=? AND dataset_id=? "
                    "AND input_version=? AND status='queued' AND attempts < max_attempts",
                    (owner, dataset_id, input_version),
                ).fetchone()[0]
            workers = min(self.max_concurrency, queued)
            if workers:
                with ThreadPoolExecutor(
                    max_workers=workers, thread_name_prefix="entity-research"
                ) as executor:
                    futures = [
                        executor.submit(self._worker, owner, dataset_id, input_version)
                        for _ in range(workers)
                    ]
                    for future in futures:
                        future.result()
            return self._read_status(owner, dataset_id, input_version)
        finally:
            self._run_lock.release()

    def retry_failed(
        self,
        *,
        owner: str,
        dataset_id: str,
        input_version: str,
        work_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Queue only selected failed items; completed claims remain immutable."""

        self._ensure_open()
        owner, dataset_id, input_version = self._scope(owner, dataset_id, input_version)
        requested = None if work_ids is None else {
            _identifier(work_id, "work_id") for work_id in work_ids
        }
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._dataset_row(conn, owner, dataset_id, input_version)
            rows = conn.execute(
                "SELECT work_id FROM entity_work_items WHERE owner=? AND dataset_id=? "
                "AND input_version=?",
                (owner, dataset_id, input_version),
            ).fetchall()
            known = {row["work_id"] for row in rows}
            if requested is not None and not requested <= known:
                conn.rollback()
                unknown = sorted(requested - known)
                raise DatasetNotFound(
                    f"Work item(s) not found in the owner-bound dataset: {unknown!r}"
                )
            now = _utc_now()
            if requested is None:
                changed = conn.execute(
                    "UPDATE entity_work_items SET status='queued',last_error=NULL,"
                    "started_at=NULL,finished_at=NULL,updated_at=? WHERE owner=? AND dataset_id=? "
                    "AND input_version=? AND status='failed' AND attempts < max_attempts",
                    (now, owner, dataset_id, input_version),
                ).rowcount
            elif not requested:
                changed = 0
            else:
                placeholders = ",".join("?" for _ in requested)
                changed = conn.execute(
                    f"UPDATE entity_work_items SET status='queued',last_error=NULL,"  # noqa: S608
                    f"started_at=NULL,finished_at=NULL,updated_at=? WHERE owner=? AND dataset_id=? "
                    f"AND input_version=? AND status='failed' AND attempts < max_attempts "
                    f"AND work_id IN ({placeholders})",
                    (now, owner, dataset_id, input_version, *sorted(requested)),
                ).rowcount
            conn.commit()
        status = self._read_status(owner, dataset_id, input_version)
        status["retried"] = changed
        return status

    def _read_status(
        self, owner: str, dataset_id: str, input_version: str
    ) -> dict[str, Any]:
        with self._connect() as conn:
            dataset = self._dataset_row(conn, owner, dataset_id, input_version)
            rows = conn.execute(
                "SELECT status,COUNT(*) AS count FROM entity_work_items WHERE owner=? "
                "AND dataset_id=? AND input_version=? GROUP BY status",
                (owner, dataset_id, input_version),
            ).fetchall()
        counts = {status: 0 for status in ("queued", "running", "completed", "failed")}
        counts.update({row["status"]: row["count"] for row in rows})
        return {
            "owner": owner,
            "dataset_id": dataset_id,
            "input_version": input_version,
            "work_items": counts,
            "resources": {
                "provider_calls_reserved": dataset["provider_calls_reserved"],
                "max_provider_calls": dataset["max_provider_calls"],
                "max_cost_micros_reserved": dataset["cost_micros_reserved"],
                "max_cost_micros": dataset["max_cost_micros"],
                "scope": "operations invoked through EntityWorkContext only",
            },
        }

    def status(
        self, *, owner: str, dataset_id: str, input_version: str
    ) -> dict[str, Any]:
        self._ensure_open()
        owner, dataset_id, input_version = self._scope(owner, dataset_id, input_version)
        return self._read_status(owner, dataset_id, input_version)

    def work_items(
        self, *, owner: str, dataset_id: str, input_version: str
    ) -> list[EntityWorkItem]:
        self._ensure_open()
        owner, dataset_id, input_version = self._scope(owner, dataset_id, input_version)
        with self._connect() as conn:
            self._dataset_row(conn, owner, dataset_id, input_version)
            rows = conn.execute(
                "SELECT * FROM entity_work_items WHERE owner=? AND dataset_id=? "
                "AND input_version=? ORDER BY work_id",
                (owner, dataset_id, input_version),
            ).fetchall()
        return [self._item_from_row(row) for row in rows]

    def results(
        self, *, owner: str, dataset_id: str, input_version: str
    ) -> list[EntityResearchClaim]:
        self._ensure_open()
        owner, dataset_id, input_version = self._scope(owner, dataset_id, input_version)
        with self._connect() as conn:
            self._dataset_row(conn, owner, dataset_id, input_version)
            rows = conn.execute(
                "SELECT claim_json FROM entity_work_items WHERE owner=? AND dataset_id=? "
                "AND input_version=? AND status='completed' ORDER BY work_id",
                (owner, dataset_id, input_version),
            ).fetchall()
        return [EntityResearchClaim.model_validate_json(row["claim_json"]) for row in rows]

    def _ensure_open(self) -> None:
        if self._closing or self._closed:
            raise EntityResearchError("Entity research runner is closed.")

    def close(self) -> None:
        # Wait for a bounded run_pending call to join all of its workers before
        # releasing the process lock. A second runner must never recover items
        # that the first runner is still actively executing.
        with self._close_lock:
            if self._closed:
                return
            self._closing = True
            with self._run_lock:
                _release_process_lock(self._process_lock)
                self._closed = True

    def __enter__(self) -> "EntityResearchRunner":
        return self

    def __exit__(self, *_args) -> None:
        self.close()


__all__ = [
    "DatasetConflict",
    "DatasetNotFound",
    "EntityResearchClaim",
    "EntityResearchError",
    "EntityResearchLimits",
    "EntityResearchRunner",
    "EntityResearchTask",
    "EntityWorkContext",
    "EntityWorkItem",
    "ResourceLimitExceeded",
]
