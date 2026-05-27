"""Compare spider-qwen output to an external baseline (e.g. b2b-scrape).

Baseline format: a JSON list of {query, vendor_name, website, email} records.
Reports overlap of discovered vendor domains for parity/regression tracking.
The baseline file is optional; when absent this is a no-op with a clear message.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def _domain(url: str | None) -> str:
    if not url:
        return ""
    host = urlparse(url).netloc.lower() or url.lower()
    return host[4:] if host.startswith("www.") else host


def compare(baseline_path: str | Path, offline: bool = True) -> dict[str, Any]:
    path = Path(baseline_path)
    if not path.exists():
        return {"status": "skipped", "reason": f"baseline file not found: {path}"}

    from .evaluate_service_mode import _build_controller

    baseline = json.loads(path.read_text(encoding="utf-8"))
    controller = _build_controller(offline)

    rows: list[dict[str, Any]] = []
    for record in baseline:
        query = record["query"]
        result = asyncio.run(controller.run(query, mode="auto"))
        ours = {_domain(c.get("website")) for c in result.validated_candidates}
        baseline_domain = _domain(record.get("website"))
        rows.append(
            {
                "query": query,
                "baseline_domain": baseline_domain,
                "matched": baseline_domain in ours if baseline_domain else False,
                "our_domains": sorted(d for d in ours if d),
            }
        )

    n = len(rows) or 1
    return {
        "status": "ok",
        "cases": len(rows),
        "domain_parity_rate": round(sum(r["matched"] for r in rows) / n, 3),
        "details": rows,
    }
