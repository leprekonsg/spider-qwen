"""Supplier network graph rendering."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from .ledger import EvidenceLedger


def render_supplier_graph(ledger: EvidenceLedger) -> str:
    """Render a Mermaid graph with vendors/pages and claim evidence edges."""
    lines = ["graph LR"]
    seen_nodes: set[str] = set()
    for item in ledger.items():
        vendor = _host(item.final_url or item.url)
        if not vendor:
            continue
        vendor_id = _node_id("vendor_" + vendor)
        claim = item.metadata.get("extraction") or item.metadata.get("field")
        if vendor_id not in seen_nodes:
            lines.append(f'  {vendor_id}["{_escape(vendor)}"]')
            seen_nodes.add(vendor_id)
        if claim:
            claim_id = _node_id(f"{claim}_{item.ledger_id}")
            lines.append(f'  {claim_id}["{_escape(str(claim))}"]')
            lines.append(f'  {vendor_id} -->|{_escape(item.source_tool)}| {claim_id}')
        else:
            page_id = _node_id("page_" + item.ledger_id)
            lines.append(f'  {page_id}["{_escape(item.source_tool)}"]')
            lines.append(f"  {vendor_id} --> {page_id}")
    return "\n".join(lines) + "\n"


def _host(url: str) -> str:
    host = urlparse(url or "").netloc or url
    return host[4:] if host.startswith("www.") else host


def _node_id(raw: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", raw)


def _escape(text: str) -> str:
    # Strip characters that can break Mermaid node syntax or inject markup when
    # the .mmd is rendered with mermaid.js (htmlLabels). Render with
    # securityLevel: 'strict' as well if displaying untrusted graphs in a browser.
    return re.sub(r'[\[\]{}<>|"`\r\n]', " ", text or "")[:80]
