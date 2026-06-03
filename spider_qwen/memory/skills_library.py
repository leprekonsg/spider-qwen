"""T-4.2: Voyager-style skill library.

A successful query becomes a parameterized, reusable *skill*: its query is
"skeletonized" (region names -> ``{region}``, integers -> ``{number}``) and, when
the same skeleton recurs across >= ``min_support`` episodes, it is stored as a
Skill keyed by that skeleton. Retrieval skeletonizes the incoming query and
matches; an optional ``embedder`` (text-embedding-v4 in production) is an
injectable seam for semantic retrieval and is OFF by default so the offline path
stays deterministic and network-free.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

from .. import SCHEMA_VERSION
from ..evidence.models import utc_now_iso
from .episodic import EpisodicRecord

# Region surfaces masked to a {region} parameter (SEA-first plus near neighbours).
_REGION_WORDS = (
    "singapore", "malaysia", "vietnam", "thailand", "indonesia", "philippines",
    "cambodia", "myanmar", "laos", "brunei", "china", "india", "japan", "taiwan",
)


class Skill(BaseModel):
    schema_version: str = SCHEMA_VERSION
    skill_id: str = Field(default_factory=lambda: f"skill_{uuid4().hex[:12]}")
    name: str
    mode: str
    template: str  # the skeletonized, parameterized query
    parameters: list[str] = Field(default_factory=list)
    support: int = 1
    source_episode_ids: list[str] = Field(default_factory=list)
    embedding: list[float] | None = None
    use_count: int = 0
    created_at: str = Field(default_factory=utc_now_iso)


def _skeletonize(query: str) -> tuple[str, dict[str, list[str]]]:
    s = query.lower().strip()
    params: dict[str, list[str]] = {"region": [], "number": []}
    for region in _REGION_WORDS:
        if re.search(rf"\b{region}\b", s):
            params["region"].append(region)
            s = re.sub(rf"\b{region}\b", "{region}", s)
    params["number"] = re.findall(r"\d+", s)
    s = re.sub(r"\d+", "{number}", s)
    return re.sub(r"\s+", " ", s).strip(), params


def _placeholders(skeleton: str) -> list[str]:
    seen: list[str] = []
    for ph in re.findall(r"\{(\w+)\}", skeleton):
        if ph not in seen:
            seen.append(ph)
    return seen


def _render_name(skeleton: str) -> str:
    return skeleton.replace("{region}", "region R").replace("{number}", "N")


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


class SkillLibrary:
    def __init__(self, state_dir: str | Path | None = None) -> None:
        self._state_dir = Path(state_dir) if state_dir else None
        self._skills: list[Skill] = []
        self._load()

    def _path(self) -> Path | None:
        if not self._state_dir:
            return None
        return self._state_dir / "memory" / "skills.jsonl"

    def _load(self) -> None:
        path = self._path()
        if path and path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._skills.append(Skill.model_validate_json(line))

    def all(self) -> list[Skill]:
        return list(self._skills)

    def get(self, skill_id: str) -> Skill | None:
        return next((s for s in self._skills if s.skill_id == skill_id), None)

    def add(self, skill: Skill) -> Skill:
        self._skills.append(skill)
        self._persist(skill)
        return skill

    def induce_from_episodes(
        self,
        episodes: list[EpisodicRecord],
        *,
        min_support: int = 3,
        embedder=None,
    ) -> list[Skill]:
        groups: dict[tuple[str, str], list[EpisodicRecord]] = {}
        for ep in episodes:
            skel, _ = _skeletonize(ep.query)
            groups.setdefault((ep.mode, skel), []).append(ep)

        induced: list[Skill] = []
        existing = {(s.mode, s.template) for s in self._skills}
        for (mode, skel), members in groups.items():
            if len(members) < min_support or (mode, skel) in existing:
                continue
            skill = Skill(
                name=_render_name(skel),
                mode=mode,
                template=skel,
                parameters=_placeholders(skel),
                support=len(members),
                source_episode_ids=[e.episode_id for e in members],
                embedding=embedder(skel) if embedder is not None else None,
            )
            self.add(skill)
            induced.append(skill)
        return induced

    def retrieve(self, query: str, *, mode: str | None = None, top_k: int = 5, embedder=None) -> list[Skill]:
        cands = [s for s in self._skills if mode is None or s.mode == mode]
        if not cands:
            return []
        if embedder is not None:
            qv = embedder(query)
            scored = [( _cosine(qv, s.embedding or embedder(s.template)), s) for s in cands]
            scored.sort(key=lambda x: (x[0], x[1].support), reverse=True)
            return [s for score, s in scored if score > 0][:top_k]

        skel, _ = _skeletonize(query)
        exact = [s for s in cands if s.template == skel]
        if exact:
            return sorted(exact, key=lambda s: s.support, reverse=True)[:top_k]
        # Lexical fallback over masked tokens (Jaccard).
        qt = set(skel.split())
        scored = []
        for s in cands:
            st = set(s.template.split())
            overlap = len(qt & st) / max(1, len(qt | st))
            if overlap >= 0.5:
                scored.append((overlap, s))
        scored.sort(key=lambda x: (x[0], x[1].support), reverse=True)
        return [s for _ov, s in scored][:top_k]

    def _persist(self, skill: Skill) -> None:
        path = self._path()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(skill.model_dump()) + "\n")
