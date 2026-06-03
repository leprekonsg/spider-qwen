"""T-7.2: project-level Qwen Agent Skills (.qwen/skills/<name>/SKILL.md).

Skills are declarative capability cards (YAML frontmatter + ## Instructions +
## Examples). The registry discovers them, the planner auto-selects by
description, and the CLI lists/matches/shows them. Deterministic + offline:
selection is term-overlap, no LLM in the hot path.
"""

from __future__ import annotations

import json

from spider_qwen import SCHEMA_VERSION
from spider_qwen.agent.planner import Planner
from spider_qwen.api.cli import main
from spider_qwen.skills.registry import SkillRegistry, load_skills

HERO = "find a replacement for an obsolete Hirose DF13-6P-1.25DSA, deliver to Singapore in 14 days"
EXPECTED_NEW = {"mpn-canonicalize", "obsolescence-triage", "fff-substitute-judge", "counterfeit-risk-check"}


# --- authored skills (real .qwen/skills) ----------------------------------

def test_authored_skills_are_discoverable():
    skills = load_skills()
    names = {s.name for s in skills}
    assert EXPECTED_NEW <= names
    assert len(skills) >= 4
    for s in skills:
        assert s.name and s.description
        assert s.schema_version == SCHEMA_VERSION


def test_frontmatter_skill_fields_parsed():
    mpn = SkillRegistry.load().get("mpn-canonicalize")
    assert mpn is not None
    assert "part" in mpn.description.lower() or "mpn" in mpn.description.lower()
    assert mpn.instructions  # body section parsed
    assert isinstance(mpn.allowed_tools, list)


def test_frontmatterless_legacy_skill_still_loads():
    qc = SkillRegistry.load().get("procurement-quote-channel")
    assert qc is not None  # name derived from the directory
    assert "quot" in qc.description.lower()  # description from the first prose line


def test_registry_match_by_description():
    matches = SkillRegistry.load().match("obsolete part lifecycle EOL NRND")
    assert matches
    assert matches[0].skill.name == "obsolescence-triage"
    assert matches[0].score >= 1


def test_unrelated_query_matches_nothing():
    assert SkillRegistry.load().match("sunny weather picnic tomorrow") == []


def test_planner_auto_invokes_skill_on_hero_query():
    selected = Planner().select_skills(HERO)
    names = {s.name for s in selected}
    assert names  # at least one auto-invoked by description
    assert names & {"obsolescence-triage", "fff-substitute-judge"}
    assert names <= {s.name for s in load_skills()}


# --- isolated parsing (tmp dir) -------------------------------------------

def _write_skill(root, name, text):
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(text, encoding="utf-8")


def test_loader_parses_frontmatter_and_sections(tmp_path):
    _write_skill(
        tmp_path, "demo",
        "---\n"
        "name: demo\n"
        "description: Demo skill for testing\n"
        "keywords: [alpha, beta]\n"
        "allowedTools: [search, fetch]\n"
        "paths: [spider_qwen/x]\n"
        "---\n\n"
        "## Instructions\n\nDo the demo thing.\n\n"
        "## Examples\n\nInput -> output.\n",
    )
    skills = load_skills(root=tmp_path)
    assert len(skills) == 1
    s = skills[0]
    assert s.name == "demo"
    assert s.description == "Demo skill for testing"
    assert s.allowed_tools == ["search", "fetch"]
    assert s.paths == ["spider_qwen/x"]
    assert "demo thing" in s.instructions
    assert "Input -> output" in s.examples


def test_loader_derives_from_frontmatterless(tmp_path):
    _write_skill(tmp_path, "legacy", "# Legacy Title\n\nUse when doing legacy things.\n\nMore body.\n")
    s = load_skills(root=tmp_path)[0]
    assert s.name == "legacy"
    assert s.description == "Use when doing legacy things."
    assert "More body" in s.instructions


def test_malformed_frontmatter_degrades_not_crashes(tmp_path):
    # An unclosed flow list is invalid YAML. One bad card must not take down the
    # whole registry: it degrades to frontmatter-less parsing and stays discoverable.
    _write_skill(
        tmp_path, "broken",
        "---\n"
        "name: broken\n"
        "keywords: [unclosed, list\n"
        "---\n\n"
        "Use when the frontmatter is malformed.\n",
    )
    _write_skill(tmp_path, "good", "---\nname: good\ndescription: A valid skill\n---\n\n## Instructions\n\nFine.\n")
    skills = load_skills(root=tmp_path)
    by_name = {s.name for s in skills}
    assert "good" in by_name  # the valid sibling still loads
    broken = next(s for s in skills if s.name == "broken")  # name derived from the directory
    assert broken.description == "Use when the frontmatter is malformed."


# --- CLI ------------------------------------------------------------------

def _cli(capsys, argv):
    rc = main(argv)
    out = capsys.readouterr().out
    assert rc == 0, out
    return json.loads(out)


def test_cli_skills_list(capsys):
    rows = _cli(capsys, ["skills", "list"])
    assert EXPECTED_NEW <= {r["name"] for r in rows}


def test_cli_skills_match_hero(capsys):
    rows = _cli(capsys, ["skills", "match", HERO])
    assert rows
    assert all("score" in r and r["score"] >= 1 for r in rows)


def test_cli_skills_show(capsys):
    row = _cli(capsys, ["skills", "show", "obsolescence-triage"])
    assert row["name"] == "obsolescence-triage"
    assert row["instructions"]
