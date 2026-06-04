"""T-7.2: project-level Qwen Agent Skills.

Skills live as ``.qwen/skills/<name>/SKILL.md`` capability cards (YAML
frontmatter -- name/description/keywords/allowedTools/paths -- plus
``## Instructions`` and ``## Examples``). ``registry`` discovers them and
matches by description (deterministic term-overlap, offline-safe); the planner
auto-selects them per query and the CLI lists/matches/shows them.

This is distinct from ``memory.skills_library`` (T-4.2), which induces
parameterized query *templates* from past episodes.
"""
