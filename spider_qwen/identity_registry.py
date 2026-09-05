"""Persistent, evidence-backed supplier identity aliases."""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, TypeVar
from uuid import uuid4

from pydantic import BaseModel, Field

from . import SCHEMA_VERSION
from .evidence.models import EvidenceRef, utc_now_iso

_T = TypeVar("_T")
_LOCKS_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    key = str(path.resolve())
    with _LOCKS_GUARD:
        thread_lock = _THREAD_LOCKS.setdefault(key, threading.RLock())
    with thread_lock:
        with path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class SupplierIdentityAlias(BaseModel):
    """An approved assertion that one supplier ID resolves to another."""

    alias_supplier_id: str
    canonical_supplier_id: str
    evidence_refs: list[EvidenceRef] = Field(min_length=1)
    approved_at: str = Field(default_factory=utc_now_iso)


class SupplierIdentityRegistry:
    """Resolve only explicitly approved, evidence-backed supplier aliases.

    ``state_dir`` is already owner-scoped by ``RunService``. The registry never
    infers aliases from names or domains, so same-name suppliers remain distinct.
    """

    def __init__(self, state_dir: str | Path | None = None) -> None:
        self._state_dir = Path(state_dir) if state_dir else None
        self._aliases: dict[str, SupplierIdentityAlias] = {}
        self._load()

    def _path(self) -> Path | None:
        if self._state_dir is None:
            return None
        return self._state_dir / "memory" / "supplier_identities.json"

    def _lock_path(self) -> Path | None:
        path = self._path()
        return path.with_suffix(".lock") if path else None

    @staticmethod
    def _validate_id(value: str, field_name: str) -> str:
        supplier_id = str(value or "").strip()
        if not supplier_id:
            raise ValueError(f"{field_name} must be a non-empty supplier ID")
        return supplier_id

    @staticmethod
    def _resolve_from(supplier_id: str, aliases: dict[str, SupplierIdentityAlias]) -> str:
        current = supplier_id
        visited: set[str] = set()
        while current in aliases:
            if current in visited:
                raise ValueError(f"Supplier identity registry contains an alias cycle at {current!r}")
            visited.add(current)
            current = aliases[current].canonical_supplier_id
        return current

    @classmethod
    def _normalize(cls, aliases: dict[str, SupplierIdentityAlias]) -> None:
        for alias_id, record in aliases.items():
            canonical = cls._resolve_from(record.canonical_supplier_id, aliases)
            if canonical == alias_id:
                raise ValueError(f"Supplier identity registry contains an alias cycle at {alias_id!r}")
            record.canonical_supplier_id = canonical

    def _read_unlocked(self) -> dict[str, SupplierIdentityAlias]:
        path = self._path()
        if path is None or not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Supplier identity registry {path} is unreadable: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
            version = payload.get("schema_version") if isinstance(payload, dict) else None
            raise ValueError(
                f"Supplier identity registry {path} contains schema_version {version!r}; "
                f"expected {SCHEMA_VERSION!r}"
            )
        rows = payload.get("aliases")
        if not isinstance(rows, list):
            raise ValueError(f"Supplier identity registry {path} must contain an aliases list")
        aliases: dict[str, SupplierIdentityAlias] = {}
        for raw in rows:
            record = SupplierIdentityAlias.model_validate(raw)
            alias_id = self._validate_id(record.alias_supplier_id, "alias_supplier_id")
            canonical_id = self._validate_id(
                record.canonical_supplier_id, "canonical_supplier_id"
            )
            if alias_id == canonical_id:
                raise ValueError(
                    f"Supplier identity registry {path} maps {alias_id!r} to itself"
                )
            record.alias_supplier_id = alias_id
            record.canonical_supplier_id = canonical_id
            if alias_id in aliases:
                raise ValueError(
                    f"Supplier identity registry {path} repeats alias "
                    f"{alias_id!r}"
                )
            aliases[alias_id] = record
        self._normalize(aliases)
        return aliases

    def _write_unlocked(self, aliases: dict[str, SupplierIdentityAlias]) -> None:
        path = self._path()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
        payload = json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "aliases": [
                    aliases[key].model_dump(mode="json") for key in sorted(aliases)
                ],
            },
            indent=2,
            ensure_ascii=False,
        )
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            for attempt in range(8):
                try:
                    os.replace(temporary, path)
                    break
                except PermissionError:
                    if attempt == 7:
                        raise
                    time.sleep(0.01 * (attempt + 1))
        finally:
            if temporary.exists():
                temporary.unlink()

    def _load(self) -> None:
        lock_path = self._lock_path()
        if lock_path is None:
            return
        with _exclusive_file_lock(lock_path):
            self._aliases = self._read_unlocked()

    def _transaction(
        self,
        mutation: Callable[[dict[str, SupplierIdentityAlias]], _T],
    ) -> _T:
        lock_path = self._lock_path()
        if lock_path is None:
            result = mutation(self._aliases)
            self._normalize(self._aliases)
            return result
        with _exclusive_file_lock(lock_path):
            self._aliases = self._read_unlocked()
            result = mutation(self._aliases)
            self._normalize(self._aliases)
            self._write_unlocked(self._aliases)
            return result

    def approve_alias(
        self,
        alias_supplier_id: str,
        canonical_supplier_id: str,
        *,
        evidence_refs: list[EvidenceRef],
    ) -> SupplierIdentityAlias:
        """Persist an explicit alias approval supported by evidence."""

        alias_id = self._validate_id(alias_supplier_id, "alias_supplier_id")
        canonical_id = self._validate_id(canonical_supplier_id, "canonical_supplier_id")
        if alias_id == canonical_id:
            raise ValueError("alias_supplier_id and canonical_supplier_id must differ")
        refs = [
            ref if isinstance(ref, EvidenceRef) else EvidenceRef.model_validate(ref)
            for ref in evidence_refs
        ]
        if not refs:
            raise ValueError("Supplier identity alias approval requires at least one evidence_ref")

        def mutation(aliases: dict[str, SupplierIdentityAlias]) -> SupplierIdentityAlias:
            target = self._resolve_from(canonical_id, aliases)
            if target == alias_id:
                raise ValueError(
                    f"Approving {alias_id!r} as an alias of {canonical_id!r} would create a cycle"
                )
            existing = aliases.get(alias_id)
            if existing is not None:
                existing_target = self._resolve_from(existing.canonical_supplier_id, aliases)
                if existing_target != target:
                    raise ValueError(
                        f"Supplier alias {alias_id!r} already resolves to {existing_target!r}; "
                        f"cannot remap it to {target!r} without explicit registry repair"
                    )
                seen = {ref.ledger_id for ref in existing.evidence_refs}
                for ref in refs:
                    if ref.ledger_id in seen:
                        continue
                    existing.evidence_refs.append(ref)
                    seen.add(ref.ledger_id)
                return existing
            record = SupplierIdentityAlias(
                alias_supplier_id=alias_id,
                canonical_supplier_id=target,
                evidence_refs=refs,
            )
            aliases[alias_id] = record
            return record

        return self._transaction(mutation)

    def resolve(self, supplier_id: str | None) -> str | None:
        """Return the approved canonical ID, preserving ``None``/blank as unbound."""

        supplier_id = str(supplier_id or "").strip()
        if not supplier_id:
            return None
        lock_path = self._lock_path()
        if lock_path is not None:
            with _exclusive_file_lock(lock_path):
                self._aliases = self._read_unlocked()
        return self._resolve_from(supplier_id, self._aliases)

    def equivalent(self, left_supplier_id: str | None, right_supplier_id: str | None) -> bool:
        """Return true only when both IDs are bound to the same canonical supplier."""

        left = str(left_supplier_id or "").strip()
        right = str(right_supplier_id or "").strip()
        if not left or not right:
            return False
        lock_path = self._lock_path()
        if lock_path is not None:
            with _exclusive_file_lock(lock_path):
                self._aliases = self._read_unlocked()
                return self._resolve_from(left, self._aliases) == self._resolve_from(
                    right, self._aliases
                )
        return self._resolve_from(left, self._aliases) == self._resolve_from(
            right, self._aliases
        )

    def aliases(self) -> list[SupplierIdentityAlias]:
        lock_path = self._lock_path()
        if lock_path is not None:
            with _exclusive_file_lock(lock_path):
                self._aliases = self._read_unlocked()
        return [self._aliases[key].model_copy(deep=True) for key in sorted(self._aliases)]


__all__ = ["SupplierIdentityAlias", "SupplierIdentityRegistry"]
