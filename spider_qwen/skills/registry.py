"""T-7.2: discover and match project-level Qwen Agent Skills.

Skills live at ``.qwen/skills/<name>/SKILL.md`` as YAML frontmatter
(name, description, keywords, allowedTools, paths) + ``## Instructions`` +
``## Examples``. Frontmatter-less legacy files still load (name from the
directory, description from the first prose line). Selection is deterministic
term-overlap over the skill name + description + keywords -- no LLM in the hot
path, so it stays offline-safe and reproducible.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel

from .models import AgentSkill

# spider_qwen/skills/registry.py -> parents[2] == repo root (mirrors tools/qwen_skills).
_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_SKILLS_DIR = _ROOT / ".qwen" / "skills"

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.DOTALL)
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    {
        "a", "an", "the", "for", "to", "in", "of", "and", "or", "with", "on",
        "at", "is", "are", "be", "by", "from", "as", "into", "this", "that",
        "it", "its", "than", "then",
    }
)


class SkillMatch(BaseModel):
    skill: AgentSkill
    score: float


def _section(body: str, heading: str) -> str:
    """Return the text under a ``## Heading`` (any level), until the next heading."""
    pat = re.compile(
        rf"^#{{1,6}}\s+{re.escape(heading)}\s*$(.*?)(?=^#{{1,6}}\s+|\Z)",
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    m = pat.search(body)
    return m.group(1).strip() if m else ""


def _first_prose_line(body: str) -> str:
    for line in body.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            return s
    return ""


def parse_skill(path: Path) -> AgentSkill:
    raw = path.read_text(encoding="utf-8")
    fm: dict = {}
    body = raw
    m = _FRONTMATTER_RE.match(raw)
    if m:
        body = m.group(2)  # keep the body even if the frontmatter is unparseable
        try:
            loaded = yaml.safe_load(m.group(1))
        except yaml.YAMLError:
            loaded = None  # malformed frontmatter -> degrade to frontmatter-less parsing
        if isinstance(loaded, dict):
            fm = loaded
    instructions = _section(body, "Instructions") or body.strip()
    return AgentSkill(
        name=str(fm.get("name") or path.parent.name),
        description=str(fm.get("description") or _first_prose_line(body)),
        keywords=[str(k) for k in (fm.get("keywords") or [])],
        allowed_tools=[str(t) for t in (fm.get("allowedTools") or fm.get("allowed_tools") or [])],
        paths=[str(p) for p in (fm.get("paths") or [])],
        instructions=instructions,
        examples=_section(body, "Examples"),
        source=path.parent.name,
    )


def load_skills(root: str | Path | None = None) -> list[AgentSkill]:
    base = Path(root) if root is not None else _DEFAULT_SKILLS_DIR
    if not base.exists():
        return []
    skills: list[AgentSkill] = []
    for sub in sorted(base.iterdir()):
        md = sub / "SKILL.md"
        if sub.is_dir() and md.exists():
            skills.append(parse_skill(md))
    return skills


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(text.lower()) if len(t) >= 2 and t not in _STOPWORDS}


def _skill_tokens(skill: AgentSkill) -> set[str]:
    toks = _tokens(skill.name) | _tokens(skill.description)
    for kw in skill.keywords:
        toks |= _tokens(kw)
    return toks


class SkillRegistry:
    def __init__(self, skills: list[AgentSkill]):
        self._skills = list(skills)

    @classmethod
    def load(cls, root: str | Path | None = None) -> SkillRegistry:
        return cls(load_skills(root))

    def all(self) -> list[AgentSkill]:
        return list(self._skills)

    def get(self, name: str) -> AgentSkill | None:
        return next((s for s in self._skills if s.name == name), None)

    def match(self, query: str, *, top_k: int = 5, min_score: float = 1.0) -> list[SkillMatch]:
        """Skills whose name/description/keywords overlap the query, best-first."""
        q = _tokens(query)
        scored = [
            SkillMatch(skill=s, score=float(len(q & _skill_tokens(s)))) for s in self._skills
        ]
        scored = [m for m in scored if m.score >= min_score]
        scored.sort(key=lambda m: (-m.score, m.skill.name))
        return scored[:top_k]
