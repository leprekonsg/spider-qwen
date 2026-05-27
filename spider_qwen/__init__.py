"""spider-qwen: deterministic, evidence-first procurement research agent.

v1 scope: search -> fetch -> extract -> rank -> RFQ draft -> persist evidence.
No portal submission, no browser automation, no code interpreter.
"""

from __future__ import annotations

__version__ = "0.1.0"

SCHEMA_VERSION = "1.0"

__all__ = ["__version__", "SCHEMA_VERSION"]
