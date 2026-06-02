"""T-3.1: supplier-part property graph (LPG) over SQLite.

One embedded, file-based store (no extra service, no Docker; ``--offline`` stays
network-free): `schema` holds the DDL + canonical key helpers, `store` is the
SQLite-backed graph with recursive-CTE traversal, `extract` turns page text into
verified triples (Generator -> Verifier -> Pruner) before upsert.
"""

from __future__ import annotations
