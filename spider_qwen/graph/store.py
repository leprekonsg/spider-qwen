"""T-3.1: SQLite-backed property-graph store.

A thin store over two tables (see ``schema``). Bounded multi-hop traversal is a
recursive CTE -- no graph engine needed for the <=2-3 hop queries spider-qwen
makes. Deep/unbounded traversal over a dense graph would get slow in CTEs and
SQLite handles concurrent writers poorly; both are fine at single-agent scale.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema import CREATE_SQL, CREATE_VIEWS_SQL, REL_TYPES


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class GraphStore:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.conn = sqlite3.connect(str(path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(CREATE_SQL)
        self._migrate_valid_to()  # T-4.3: add valid_to to pre-existing graph files
        self.conn.executescript(CREATE_VIEWS_SQL)  # view created after the column exists
        self.conn.commit()

    def _migrate_valid_to(self) -> None:
        cols = {row[1] for row in self.conn.execute("PRAGMA table_info(edges)").fetchall()}
        if "valid_to" not in cols:
            self.conn.execute("ALTER TABLE edges ADD COLUMN valid_to TEXT")

    # --- writes ------------------------------------------------------------

    def upsert_node(self, node_id: str, node_type: str, props: dict[str, Any] | None = None) -> None:
        existing = self.get_node(node_id)
        merged = {**(existing["props"] if existing else {}), **(props or {})}
        self.conn.execute(
            "INSERT INTO nodes(id, type, props) VALUES(?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET type=excluded.type, props=excluded.props",
            (node_id, node_type, json.dumps(merged)),
        )
        self.conn.commit()

    def add_edge(
        self,
        src: str,
        dst: str,
        rel: str,
        *,
        confidence: float,
        reliability: float,
        evidence_claim_id: str,
        event_ts: str | None = None,
        ingest_ts: str | None = None,
        valid_to: str | None = None,
        grade: str | None = None,
        props: dict[str, Any] | None = None,
    ) -> None:
        if not evidence_claim_id:
            raise ValueError(
                "add_edge requires evidence_claim_id (a ledger_id). Every graph edge "
                "must reference its asserting claim; never persist an edge from a bare URL."
            )
        self.conn.execute(
            "INSERT INTO edges(src, dst, rel, confidence, reliability, evidence_claim_id, "
            "event_ts, ingest_ts, valid_to, grade, props) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(src, dst, rel, evidence_claim_id) DO UPDATE SET "
            "confidence=excluded.confidence, reliability=excluded.reliability, "
            "event_ts=excluded.event_ts, ingest_ts=excluded.ingest_ts, "
            "valid_to=excluded.valid_to, grade=excluded.grade, props=excluded.props",
            (src, dst, rel, float(confidence), float(reliability), evidence_claim_id,
             event_ts, ingest_ts or _now_iso(), valid_to, grade, json.dumps(props or {})),
        )
        self.conn.commit()

    # --- reads -------------------------------------------------------------

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT id, type, props FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        if row is None:
            return None
        return {"id": row[0], "type": row[1], "props": json.loads(row[2] or "{}")}

    def neighbors(self, node_id: str, rels: tuple[str, ...] | list[str] | None = None) -> list[dict[str, Any]]:
        """Current (non-superseded) out-edges of a node. Bi-temporal history is
        reached via ``versions``; superseded rows are excluded here so retrieval
        never routes through a closed fact."""
        rels = tuple(rels) if rels else REL_TYPES
        placeholders = ",".join("?" * len(rels))
        rows = self.conn.execute(
            f"SELECT src, dst, rel, confidence, reliability, evidence_claim_id, grade "
            f"FROM edges WHERE src = ? AND rel IN ({placeholders}) AND valid_to IS NULL",
            (node_id, *rels),
        ).fetchall()
        return [self._edge_row(r) for r in rows]

    def edges(self) -> list[dict[str, Any]]:
        """Current (non-superseded) edges only; see ``versions`` for full history."""
        rows = self.conn.execute(
            "SELECT src, dst, rel, confidence, reliability, evidence_claim_id, grade "
            "FROM edges WHERE valid_to IS NULL"
        ).fetchall()
        return [self._edge_row(r) for r in rows]

    def node_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]

    def edge_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]

    # --- bi-temporal (T-4.3) ----------------------------------------------

    _FULL_COLS = (
        "src, dst, rel, confidence, reliability, evidence_claim_id, "
        "event_ts, ingest_ts, valid_to, grade, props"
    )

    def versions(self, src: str, dst: str, rel: str) -> list[dict[str, Any]]:
        """All temporal versions of an edge key, oldest valid-from first."""
        rows = self.conn.execute(
            f"SELECT {self._FULL_COLS} FROM edges WHERE src=? AND dst=? AND rel=? "
            "ORDER BY COALESCE(event_ts, '') ASC, ingest_ts ASC",
            (src, dst, rel),
        ).fetchall()
        return [self._full_edge_row(r) for r in rows]

    def current_edges(self) -> list[dict[str, Any]]:
        """Open (non-superseded) edges via the edges_current view."""
        rows = self.conn.execute(f"SELECT {self._FULL_COLS} FROM edges_current").fetchall()
        return [self._full_edge_row(r) for r in rows]

    def mark_superseded(self, src: str, dst: str, rel: str, *, valid_to: str, before: str | None = None) -> None:
        """Close open rows for an edge key. With ``before`` set, only rows whose
        valid_from precedes it are closed (so a simultaneous fact stays open)."""
        if before is None:
            self.conn.execute(
                "UPDATE edges SET valid_to=? WHERE src=? AND dst=? AND rel=? AND valid_to IS NULL",
                (valid_to, src, dst, rel),
            )
        else:
            self.conn.execute(
                "UPDATE edges SET valid_to=? WHERE src=? AND dst=? AND rel=? AND valid_to IS NULL "
                "AND (event_ts IS NULL OR event_ts < ?)",
                (valid_to, src, dst, rel, before),
            )
        self.conn.commit()

    @staticmethod
    def _full_edge_row(r: tuple) -> dict[str, Any]:
        return {
            "src": r[0], "dst": r[1], "rel": r[2], "confidence": r[3], "reliability": r[4],
            "evidence_claim_id": r[5], "event_ts": r[6], "ingest_ts": r[7],
            "valid_to": r[8], "grade": r[9], "props": json.loads(r[10] or "{}"),
        }

    def traverse(
        self,
        start_id: str,
        rels: tuple[str, ...] | list[str] | None = None,
        max_depth: int = 2,
    ) -> list[dict[str, Any]]:
        """Bounded multi-hop traversal via a recursive CTE; returns reachable
        nodes with the human-readable relation path and hop depth."""
        rels = tuple(rels) if rels else REL_TYPES
        placeholders = ",".join("?" * len(rels))
        sql = f"""
        WITH RECURSIVE chain(id, path, depth) AS (
          SELECT id, id, 0 FROM nodes WHERE id = ?
          UNION ALL
          SELECT e.dst, chain.path || ' -> ' || e.rel || ' -> ' || e.dst, chain.depth + 1
          FROM edges e JOIN chain ON e.src = chain.id
          WHERE chain.depth < ? AND e.rel IN ({placeholders}) AND e.valid_to IS NULL
        )
        SELECT id, path, depth FROM chain WHERE depth > 0 ORDER BY depth, id
        """
        rows = self.conn.execute(sql, (start_id, max_depth, *rels)).fetchall()
        return [{"id": r[0], "path": r[1], "depth": r[2]} for r in rows]

    def close(self) -> None:
        self.conn.close()

    @staticmethod
    def _edge_row(r: tuple) -> dict[str, Any]:
        return {
            "src": r[0], "dst": r[1], "rel": r[2], "confidence": r[3],
            "reliability": r[4], "evidence_claim_id": r[5], "grade": r[6],
        }
