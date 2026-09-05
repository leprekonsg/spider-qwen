"""Reproducibility metadata for benchmark and hand-graded evaluation runs."""

from __future__ import annotations

import importlib.metadata
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from .. import SCHEMA_VERSION
from ..application.profiles import OperatorProfile, PIPELINE_VERSION


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _git_revision() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    revision = completed.stdout.strip()
    return revision or None


def _git_dirty() -> bool | None:
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return bool(completed.stdout.strip())


def _source_digest() -> str:
    """Hash executed source/config/schema files, including uncommitted edits."""
    root = Path(__file__).resolve().parents[2] / "spider_qwen"
    digest = hashlib.sha256()
    for path in sorted(
        candidate for candidate in root.rglob("*")
        if candidate.is_file() and candidate.suffix in {".py", ".json", ".yaml", ".yml"}
    ):
        digest.update(str(path.relative_to(root)).replace("\\", "/").encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def evaluation_manifest(
    profile: OperatorProfile,
    *,
    deadline_seconds: int | None = None,
    memory_condition: str = "cold_empty_state",
    page_cache_condition: str = "cold_empty_state",
) -> dict[str, Any]:
    """Describe the executable configuration without inventing unavailable data."""
    effective = profile.effective_config(deadline_seconds=deadline_seconds)
    manifest = {
        "pipeline_version": PIPELINE_VERSION,
        "code_revision": _git_revision(),
        "working_tree_dirty": _git_dirty(),
        "source_digest_sha256": _source_digest(),
        "schema_versions": {
            "run_result": SCHEMA_VERSION,
            "policy": effective.get("policy", {}).get("policy_source_sha256"),
            "prompt": "unavailable",
        },
        "operator_profile": profile.name,
        "effective_config": effective,
        "effective_config_fingerprint": effective["config_fingerprint"],
        "resolved_models": effective.get("policy", {}).get("models", {}),
        "dependencies": {
            "spider_qwen": _package_version("spider-qwen"),
            "pydantic": _package_version("pydantic"),
            "tldextract": _package_version("tldextract"),
        },
        # tldextract bundles the offline PSL snapshot, but does not expose a
        # stable snapshot revision. Record the package version and say so.
        "public_suffix_list": {
            "provider": "tldextract",
            "package_version": _package_version("tldextract"),
            "snapshot_revision": "unavailable",
        },
        "memory_condition": memory_condition,
        "page_cache_condition": page_cache_condition,
    }
    manifest["evaluation_fingerprint"] = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return manifest
