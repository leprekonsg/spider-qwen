"""T-4.2: Agent Workflow Memory (AWM) + AutoGuide guidelines.

AWM induces a reusable step *workflow* from past trajectories: the longest step
prefix shared by at least ``min_support`` trajectories becomes a stored playbook
skeleton. AutoGuide stores context-conditional guidelines (a trigger token + a
piece of advice) retrieved when the trigger appears in the current context.
Plain JSONL persistence; deterministic; no network, no LLM.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

from .. import SCHEMA_VERSION
from ..evidence.models import utc_now_iso


class Workflow(BaseModel):
    schema_version: str = SCHEMA_VERSION
    workflow_id: str = Field(default_factory=lambda: f"wf_{uuid4().hex[:12]}")
    name: str
    mode: str
    steps: list[str] = Field(default_factory=list)
    support: int = 1
    created_at: str = Field(default_factory=utc_now_iso)


class Guideline(BaseModel):
    schema_version: str = SCHEMA_VERSION
    guideline_id: str = Field(default_factory=lambda: f"guide_{uuid4().hex[:12]}")
    context: str  # trigger token matched against the current context
    advice: str
    created_at: str = Field(default_factory=utc_now_iso)


def _longest_common_prefix(seqs: list[list[str]]) -> list[str]:
    if not seqs:
        return []
    out: list[str] = []
    for i in range(min(len(s) for s in seqs)):
        column = {s[i] for s in seqs}
        if len(column) != 1:
            break
        out.append(seqs[0][i])
    return out


class WorkflowMemory:
    def __init__(self, state_dir: str | Path | None = None) -> None:
        self._state_dir = Path(state_dir) if state_dir else None
        self._workflows: list[Workflow] = []
        self._guidelines: list[Guideline] = []
        self._load()

    def _wf_path(self) -> Path | None:
        return self._state_dir / "memory" / "workflows.jsonl" if self._state_dir else None

    def _guide_path(self) -> Path | None:
        return self._state_dir / "memory" / "guidelines.jsonl" if self._state_dir else None

    def _load(self) -> None:
        wf = self._wf_path()
        if wf and wf.exists():
            self._workflows = [Workflow.model_validate_json(l) for l in wf.read_text(encoding="utf-8").splitlines() if l.strip()]
        gp = self._guide_path()
        if gp and gp.exists():
            self._guidelines = [Guideline.model_validate_json(l) for l in gp.read_text(encoding="utf-8").splitlines() if l.strip()]

    def all(self) -> list[Workflow]:
        return list(self._workflows)

    def guidelines(self) -> list[Guideline]:
        return list(self._guidelines)

    def induce(self, trajectories: list[list[str]], *, mode: str, min_support: int = 3) -> Workflow | None:
        if len(trajectories) < min_support:
            return None
        steps = _longest_common_prefix(trajectories)
        if not steps:
            return None
        wf = Workflow(
            name=f"{mode}: " + " -> ".join(steps),
            mode=mode,
            steps=steps,
            support=len(trajectories),
        )
        self._workflows.append(wf)
        self._append(self._wf_path(), wf)
        return wf

    def add_guideline(self, *, context: str, advice: str) -> Guideline:
        g = Guideline(context=context, advice=advice)
        self._guidelines.append(g)
        self._append(self._guide_path(), g)
        return g

    def guidelines_for(self, context: str) -> list[Guideline]:
        low = context.lower()
        return [g for g in self._guidelines if g.context.lower() in low]

    @staticmethod
    def _append(path: Path | None, model: BaseModel) -> None:
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(model.model_dump()) + "\n")
