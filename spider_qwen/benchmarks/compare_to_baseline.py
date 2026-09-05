"""Compare spider-qwen output to an external baseline (e.g. b2b-scrape).

Baseline format: a JSON list of {query, vendor_name, website, email} records.
Reports overlap of discovered vendor domains for parity/regression tracking.
The baseline file is optional; when absent this is a no-op with a clear message.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def _domain(url: str | None) -> str:
    if not url:
        return ""
    host = urlparse(url).netloc.lower() or url.lower()
    return host[4:] if host.startswith("www.") else host


def compare(baseline_path: str | Path, offline: bool = True, *, profile: str | None = None) -> dict[str, Any]:
    path = Path(baseline_path)
    if not path.exists():
        return {"status": "skipped", "reason": f"baseline file not found: {path}"}

    from .evaluate_service_mode import _build_controller, _profile_for
    from .manifest import evaluation_manifest

    baseline = json.loads(path.read_text(encoding="utf-8"))
    resolved_profile = _profile_for(offline, profile)

    async def run_all(controller) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        # One loop is required for a reused live controller with pooled clients.
        for record in baseline:
            result = await controller.run(record["query"], mode="auto")
            ours = {_domain(candidate.get("website")) for candidate in result.validated_candidates}
            baseline_domain = _domain(record.get("website"))
            rows.append({
                "query": record["query"],
                "baseline_domain": baseline_domain,
                "matched": baseline_domain in ours if baseline_domain else False,
                "our_domains": sorted(domain for domain in ours if domain),
            })
        return rows

    with tempfile.TemporaryDirectory(prefix="spider-qwen-baseline-") as state_dir:
        controller = _build_controller(offline, profile=resolved_profile.name, state_dir=state_dir)
        rows = asyncio.run(run_all(controller))

    n = len(rows) or 1
    return {
        "status": "ok",
        "cases": len(rows),
        "domain_parity_rate": round(sum(r["matched"] for r in rows) / n, 3),
        "evaluation_manifest": evaluation_manifest(
            resolved_profile,
            memory_condition="cold_start_then_shared_state",
            page_cache_condition="cold_start_then_shared_state",
        ),
        "details": rows,
    }
