"""T-3.1: SQLite LPG schema + canonical node-key helpers.

Two tables in one SQLite file (the same store can hold the ledger + sqlite-vec
embeddings). The asymmetric ``CROSS_REFERENCE{grade}`` edge is naturally a
directional row; bi-temporality is the ``event_ts``/``ingest_ts`` columns (T-4.3
builds on them, no new engine).
"""

from __future__ import annotations

import re

NODE_TYPES = (
    "Part", "Manufacturer", "Distributor", "Datasheet", "Parameter",
    "Package", "PCN", "Claim", "Source",
)
REL_TYPES = (
    "MANUFACTURED_BY", "STOCKED_AT", "CROSS_REFERENCE", "SUPERSEDED_BY",
    "PIN_COMPATIBLE_WITH", "SAME_DIE_AS", "AFFECTED_BY", "ACQUIRED_BY",
    "RENAMED_TO", "FRANCHISE_FOR", "CONTRADICTS",
)

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS nodes (
  id    TEXT PRIMARY KEY,
  type  TEXT NOT NULL,
  props TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS edges (
  src               TEXT NOT NULL REFERENCES nodes(id),
  dst               TEXT NOT NULL REFERENCES nodes(id),
  rel               TEXT NOT NULL,
  confidence        REAL NOT NULL,
  reliability       REAL NOT NULL,
  evidence_claim_id TEXT NOT NULL,
  event_ts          TEXT,
  ingest_ts         TEXT NOT NULL,
  grade             TEXT,
  props             TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (src, dst, rel, evidence_claim_id)
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src, rel);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst, rel);
"""

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def norm(text: str) -> str:
    return _NON_ALNUM.sub("", (text or "").lower())


def part_key(mpn: str) -> str:
    return f"part:{norm(mpn)}"


def mfr_key(name: str) -> str:
    return f"mfr:{norm(name)}"


def dist_key(name: str) -> str:
    return f"dist:{norm(name)}"
