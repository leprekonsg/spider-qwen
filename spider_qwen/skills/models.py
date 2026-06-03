"""T-7.2: typed model for a project-level Qwen Agent Skill."""

from __future__ import annotations

from pydantic import BaseModel, Field

from .. import SCHEMA_VERSION


class AgentSkill(BaseModel):
    schema_version: str = SCHEMA_VERSION
    name: str
    description: str = ""
    keywords: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    paths: list[str] = Field(default_factory=list)
    instructions: str = ""
    examples: str = ""
    source: str = ""  # directory name under .qwen/skills
