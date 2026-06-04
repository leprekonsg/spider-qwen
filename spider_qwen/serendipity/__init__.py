"""Serendipity layer: query expansion, corrective retrieval, GRAM-lite mode,
legacy-OCR mining, Wayback sourcing, source bandit, and proactive S3 signals.

Modules here are deterministic/offline-friendly by default; Qwen-backed paths are
optional enrichments that degrade to the deterministic behaviour when no API key
is configured.
"""
