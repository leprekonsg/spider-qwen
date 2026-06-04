"""Guardrail #3.1 (evidence-first) + #3.5 (offline zero-network).

Every promoted discovery slot item must carry an evidence_ref ("evidence or it
didn't happen"); and the documented hero query must exercise all three serendipity
senses with provenance -- the acceptance the build plan's section 7 demands but
no test previously covered.
"""

from __future__ import annotations

import json

from spider_qwen.api.cli import main
from spider_qwen.evidence.models import EvidenceRef
from spider_qwen.serendipity.discovery import (
    DiscoverySlot,
    DiscoverySlotItem,
    _enforce_evidence,
)

HERO_QUERY = "find a replacement for an obsolete Hirose DF13-6P-1.25DSA, deliver to Singapore in 14 days"


def _ref() -> EvidenceRef:
    return EvidenceRef(ledger_id="ev_1", url="https://x.example", snippet_hash="0" * 64,
                       retrieved_at="2026-06-03T00:00:00+00:00")


# --- the structural guard --------------------------------------------------

def test_enforce_evidence_withholds_unsourced_items():
    slot = DiscoverySlot(slot="S3", kind="risk_watch_or_dmsms", supported=True, items=[
        DiscoverySlotItem(kind="risk_watch", summary="sourced", source_component="signals", evidence_refs=[_ref()]),
        DiscoverySlotItem(kind="dmsms", summary="unsourced", source_component="dmsms", evidence_refs=[]),
    ])
    out = _enforce_evidence(slot)
    assert [it.summary for it in out.items] == ["sourced"]
    assert out.supported is True
    assert "unsourced item(s) withheld" in out.note


def test_enforce_evidence_unsupports_a_fully_unsourced_slot():
    slot = DiscoverySlot(slot="S1", kind="substitute", supported=True, items=[
        DiscoverySlotItem(kind="substitute", summary="x", source_component="graph_ppr", evidence_refs=[]),
    ])
    out = _enforce_evidence(slot)
    assert out.items == []
    assert out.supported is False
    assert "withheld" in out.note


# --- the hero query, end-to-end through the sidecar ------------------------

def test_hero_query_exercises_all_three_senses_with_evidence(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("SPIDER_QWEN_STATE_DIR", str(tmp_path))
    rc = main(["run", HERO_QUERY, "--offline", "--serendipity"])
    out = capsys.readouterr().out
    assert rc == 0, out
    sd = json.loads(out)["serendipity_discovery"]
    assert sd is not None
    slots = {s["slot"]: s for s in sd["slots"]}
    assert set(slots) == {"S1", "S2", "S3"}
    for slot in slots.values():
        assert slot["supported"], f"{slot['slot']} unsupported for the documented hero query"
        for item in slot["items"]:
            assert item["evidence_refs"], f"a promoted {slot['slot']} item carries no evidence_ref"


def test_offline_run_makes_zero_network_calls(capsys, tmp_path, monkeypatch, no_network):
    monkeypatch.setenv("SPIDER_QWEN_STATE_DIR", str(tmp_path))
    rc = main(["run", "office cleaning Singapore", "--offline"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert json.loads(out)["run_id"].startswith("run_")
