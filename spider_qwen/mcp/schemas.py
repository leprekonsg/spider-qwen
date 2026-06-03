"""T-7.1: typed input/output models for the MCP tools."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .. import SCHEMA_VERSION


class ClassifyResult(BaseModel):
    schema_version: str = SCHEMA_VERSION
    mode: str
    confidence: float = 0.0
    matched_signals: list[str] = Field(default_factory=list)
    rationale: str = ""


class EvidenceItemView(BaseModel):
    ledger_id: str
    url: str
    source_tool: str
    snippet: str = ""
    confidence: float = 0.0
    reliability: float = 1.0


class EvidenceShowResult(BaseModel):
    schema_version: str = SCHEMA_VERSION
    run_id: str
    count: int = 0
    items: list[EvidenceItemView] = Field(default_factory=list)


class EvidenceVerifyResult(BaseModel):
    schema_version: str = SCHEMA_VERSION
    run_id: str
    ok: bool = True
    checked_claims: int = 0
    valid_claims: int = 0
    chain_ok: bool = True
    chain_checked: int = 0
    issues: list[dict[str, Any]] = Field(default_factory=list)


class FactView(BaseModel):
    entity_name: str
    field: str
    value: str
    confidence: float = 0.0
    decayed_confidence: float = 0.0
    score: float = 0.0


class MemoryRecallResult(BaseModel):
    schema_version: str = SCHEMA_VERSION
    query: str
    top_k: int = 5
    count: int = 0
    facts: list[FactView] = Field(default_factory=list)


class MemoryReflectResult(BaseModel):
    schema_version: str = SCHEMA_VERSION
    count: int = 0
    insights: list[dict[str, Any]] = Field(default_factory=list)
