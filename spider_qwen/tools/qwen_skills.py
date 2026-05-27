"""Qwen Code skill prompt loader.

Runtime use is intentionally narrow: skills provide domain instructions to
Qwen-assisted calls, while deterministic code remains the source of truth.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=8)
def load_skill_prompt(skill_name: str) -> str:
    path = _ROOT / ".qwen" / "skills" / skill_name / "SKILL.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()
