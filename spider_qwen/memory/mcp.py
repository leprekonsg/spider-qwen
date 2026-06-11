"""MCP-shaped adapter for semantic memory tools.

The adapter exposes explicit tool names so the same seam can be backed by an MCP
server later without changing controller logic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..evidence.models import EvidenceRef
from .revalidation import Revalidator
from .semantic import MemoryRecall, SemanticMemory


class SemanticMemoryMcpAdapter:
    def __init__(self, state_dir: str | Path, memory: SemanticMemory | None = None) -> None:
        self.memory = memory or SemanticMemory(state_dir)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name == "semantic_memory.recall":
            recalls = self.recall(
                query=str(arguments.get("query", "")),
                top_k=int(arguments.get("top_k", 5)),
                context_budget_chars=int(arguments.get("context_budget_chars", 1200)),
                reference_ts=arguments.get("reference_ts"),
            )
            return {"recalls": [r.model_dump(mode="json") for r in recalls]}
        if name == "semantic_memory.revalidate":
            refs = [
                ref if isinstance(ref, EvidenceRef) else EvidenceRef.model_validate(ref)
                for ref in arguments.get("evidence_refs", [])
            ]
            fact = Revalidator(self.memory).revalidate(
                fact_id=str(arguments["fact_id"]),
                observed_value=str(arguments.get("observed_value", "")),
                evidence_refs=refs,
                new_confidence=float(arguments.get("new_confidence", 0.0)),
            )
            return {"fact": fact.model_dump(mode="json") if fact else None}
        raise ValueError(f"Unknown semantic memory MCP tool: {name}")

    def recall(
        self,
        *,
        query: str,
        top_k: int = 5,
        context_budget_chars: int = 1200,
        reference_ts: str | None = None,
    ) -> list[MemoryRecall]:
        return self.memory.recall(
            query,
            top_k=top_k,
            context_budget_chars=context_budget_chars,
            reference_ts=reference_ts,
        )
