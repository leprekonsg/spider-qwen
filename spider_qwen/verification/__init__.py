"""Verification spine (T-2.2).

FActScore-style atomic decomposition -> MiniCheck-style entailment gatekeeper on
every (claim, evidence_span) -> SAFE-style search-grounded re-verification of
flagged atoms. Deterministic and offline by default (value-grounding + token
overlap, matching spider-qwen's hot path); learned NLI (MiniCheck-FT5) and live
search (TinyFish) plug into the optional `model` / `search_fn` seams.

The orchestrator lives in `..evidence.verifier` (VerificationSpine) so it sits
next to the existing span-level ledger verifier.

`cove.py` (T-2.3) adds Chain-of-Verification + semantic entropy for substitute
suggestions; cross-source contradiction -> `disputed` reuses semantic memory's
conflict policy (`..memory.semantic` + `..memory.promotion.contradicts`).
"""

from __future__ import annotations
