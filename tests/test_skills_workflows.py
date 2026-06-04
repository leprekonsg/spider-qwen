"""T-4.2: Voyager skill library + Agent Workflow Memory + AutoGuide guidelines.

Acceptance: after 3 service queries that share a skeleton (ISO-certified supplier
in region R, MOQ < N), a parameterized skill is induced, stored, and retrieved
for a 4th query in a new region; AWM induces a reusable step workflow from past
trajectories; context-conditional guidelines are stored and retrieved.
"""

from __future__ import annotations

from spider_qwen.memory.episodic import EpisodicRecord
from spider_qwen.memory.skills_library import Skill, SkillLibrary
from spider_qwen.memory.workflows import Workflow, WorkflowMemory


def _ep(query: str) -> EpisodicRecord:
    return EpisodicRecord(query=query, mode="service_quote_required", summary="ok", outcome="success")


# --- Voyager skill library --------------------------------------------------

def test_skill_induced_from_three_similar_episodes_and_retrieved(tmp_path):
    lib = SkillLibrary(tmp_path)
    episodes = [
        _ep("find ISO-certified supplier in Singapore, MOQ < 500"),
        _ep("find ISO-certified supplier in Malaysia, MOQ < 1000"),
        _ep("find ISO-certified supplier in Vietnam, MOQ < 200"),
    ]
    induced = lib.induce_from_episodes(episodes, min_support=3)
    assert len(induced) == 1
    skill = induced[0]
    assert isinstance(skill, Skill)
    assert "{region}" in skill.template and "{number}" in skill.template
    assert skill.support == 3

    # 4th query in a new region must retrieve the same parameterized skill.
    hits = lib.retrieve("find ISO-certified supplier in Thailand, MOQ < 300")
    assert hits and hits[0].skill_id == skill.skill_id

    # Persistence: the induced skill survives reload.
    assert SkillLibrary(tmp_path).get(skill.skill_id) is not None


def test_no_skill_below_min_support(tmp_path):
    lib = SkillLibrary(tmp_path)
    episodes = [
        _ep("find ISO-certified supplier in Singapore, MOQ < 500"),
        _ep("find ISO-certified supplier in Malaysia, MOQ < 1000"),
    ]
    assert lib.induce_from_episodes(episodes, min_support=3) == []


def test_retrieve_uses_injected_embedder_when_present(tmp_path):
    lib = SkillLibrary(tmp_path)
    episodes = [_ep(f"calibrate flow meter in Singapore, lot {i}") for i in range(3)]
    lib.induce_from_episodes(episodes, min_support=3)
    # An injected embedder that maps everything to the same vector -> retrieval
    # falls back to the lexical path; assert the seam is accepted and used.
    calls = []
    embedder = lambda text: (calls.append(text) or [1.0, 0.0])
    hits = lib.retrieve("calibrate flow meter in Penang, lot 9", embedder=embedder)
    assert calls  # embedder was invoked
    assert hits


# --- Agent Workflow Memory + AutoGuide --------------------------------------

def test_workflow_induced_from_common_trajectory(tmp_path):
    wm = WorkflowMemory(tmp_path)
    trajectories = [
        ["classify", "search", "fetch", "extract", "rank", "rfq_draft"],
        ["classify", "search", "fetch", "extract", "rank", "rfq_draft"],
        ["classify", "search", "fetch", "extract", "rank"],
    ]
    wf = wm.induce(trajectories, mode="service_quote_required", min_support=3)
    assert isinstance(wf, Workflow)
    # The longest sequence shared by all >= min_support trajectories.
    assert wf.steps == ["classify", "search", "fetch", "extract", "rank"]
    assert WorkflowMemory(tmp_path).all()  # persisted


def test_workflow_not_induced_below_support(tmp_path):
    wm = WorkflowMemory(tmp_path)
    assert wm.induce([["classify", "search"]], mode="auto", min_support=3) is None


def test_autoguide_guidelines_are_context_conditional(tmp_path):
    wm = WorkflowMemory(tmp_path)
    wm.add_guideline(context="broker", advice="Cross-check broker quotes against ERAI before trusting.")
    wm.add_guideline(context="eol", advice="Surface the SD-22 ladder, not a single quote.")
    hits = wm.guidelines_for("found a broker source for an EOL part")
    advices = {g.advice for g in hits}
    assert "Cross-check broker quotes against ERAI before trusting." in advices
    assert "Surface the SD-22 ladder, not a single quote." in advices
