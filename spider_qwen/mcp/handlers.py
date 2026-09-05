"""T-7.1: pure, deterministic handlers backing the MCP tools.

These have no dependency on the ``mcp`` SDK and are always unit-tested. Each wraps
an existing read-only capability; none mutates files, sends RFQs, or calls a live
API. ``state_dir`` defaults to ``$SPIDER_QWEN_STATE_DIR`` (mirroring the CLI) so a
caller can point at any run's persisted evidence/memory.
"""

from __future__ import annotations

import os

from ..api.run_queries import CompletedRunQueries, Operation, load_candidate_observations

from ..evidence.ledger import EvidenceLedger
from ..evidence.verifier import verify_ledger
from ..memory.episodic import EpisodicMemory
from ..memory.reflections import ReflectionEngine
from ..memory.semantic import SemanticMemory
from ..modes.classifier import ModeClassifier
from .schemas import (
    ClassifyResult,
    EvidenceItemView,
    EvidenceShowResult,
    EvidenceVerifyResult,
    FactView,
    MemoryRecallResult,
    MemoryReflectResult,
)


def _state_dir(state_dir: str | None = None) -> str:
    return state_dir or os.getenv("SPIDER_QWEN_STATE_DIR", ".spider_qwen")


def inspect_run(run_id: str, operation: Operation, *, supplier_id: str | None = None,
                supplier_ids: list[str] | None = None, offering_id: str | None = None,
                offering_ids: list[str] | None = None, state_dir: str | None = None) -> dict:
    """Inspect a completed run in the operator-bound stdio owner's workspace."""
    from ..application.run_service import load_run_result

    owner = os.getenv("SPIDER_QWEN_MCP_OWNER", "local")
    def load(identifier: str, *, owner: str):
        return load_run_result(_state_dir(state_dir), identifier, owner=owner)
    def evidence(identifier: str, *, owner: str, ledger_ids: set[str]):
        return load_candidate_observations(_state_dir(state_dir), identifier, owner=owner, ledger_ids=ledger_ids)
    return CompletedRunQueries(load, evidence).inspect(run_id, operation, owner=owner,
                                             supplier_id=supplier_id, supplier_ids=supplier_ids,
                                             offering_id=offering_id, offering_ids=offering_ids)


def procurement_classify(query: str) -> ClassifyResult:
    """Classify a procurement query into a mode (deterministic, read-only)."""
    r = ModeClassifier().classify(query)
    signals = sorted({s for terms in r.signals.values() for s in terms})
    return ClassifyResult(
        mode=r.mode.value, confidence=r.confidence,
        matched_signals=signals, rationale=r.rationale,
    )


def evidence_show(run_id: str, *, state_dir: str | None = None) -> EvidenceShowResult:
    """List a run's evidence ledger items (read-only). Unknown run -> empty."""
    ledger = EvidenceLedger.load(run_id, _state_dir(state_dir))
    items = [
        EvidenceItemView(
            ledger_id=it.ledger_id, url=it.final_url or it.url, source_tool=it.source_tool,
            snippet=(it.snippet or "")[:200], confidence=it.confidence, reliability=it.reliability,
        )
        for it in ledger.items()
    ]
    return EvidenceShowResult(run_id=run_id, count=len(items), items=items)


def evidence_verify(run_id: str, *, state_dir: str | None = None) -> EvidenceVerifyResult:
    """Re-verify a run's evidence spans + Merkle chain (read-only)."""
    ledger = EvidenceLedger.load(run_id, _state_dir(state_dir))
    res = verify_ledger(ledger)
    chain = ledger.verify_chain()
    issues = [i.model_dump() for i in res.issues] + [i.model_dump() for i in chain.issues]
    return EvidenceVerifyResult(
        run_id=run_id, ok=res.ok and chain.ok,
        checked_claims=res.checked_claims, valid_claims=res.valid_claims,
        chain_ok=chain.ok, chain_checked=chain.checked, issues=issues,
    )


def memory_recall(query: str, *, top_k: int = 5, state_dir: str | None = None) -> MemoryRecallResult:
    """Recall evidence-backed semantic facts for a query (read-only)."""
    recalls = SemanticMemory(_state_dir(state_dir)).recall(query, top_k=top_k)
    facts = [
        FactView(
            entity_name=r.fact.entity_name, field=r.fact.field, value=r.fact.value,
            confidence=r.fact.confidence, decayed_confidence=r.decayed_confidence, score=r.score,
        )
        for r in recalls
    ]
    return MemoryRecallResult(query=query, top_k=top_k, count=len(facts), facts=facts)


def memory_reflect(*, state_dir: str | None = None) -> MemoryReflectResult:
    """Distil reflections over learned facts + episodes (read-only)."""
    sd = _state_dir(state_dir)
    insights = ReflectionEngine().reflect(SemanticMemory(sd).all(), EpisodicMemory(sd).all())
    return MemoryReflectResult(
        count=len(insights), insights=[i.model_dump(mode="json") for i in insights],
    )
