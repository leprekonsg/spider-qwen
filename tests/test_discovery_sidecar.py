"""T-8.2: --serendipity discovery sidecar.

Populates S1/S2/S3 from the REAL components (graph PPR substitutes, long-tail /
archival sources, signals + DMSMS risk) over a run's already-fetched ledger.
Default run() is unchanged; the sidecar consumes no new fetch/search budget, so
it cannot starve verification. Every promoted slot item carries evidence_refs +
a source_component; partial/unsupported discoveries are marked, not promoted;
substitutes still pass CoVe verification.
"""

from __future__ import annotations

import asyncio
import json

from spider_qwen.agent.controller import Controller
from spider_qwen.api.cli import main
from spider_qwen.evidence.models import EvidenceItem
from spider_qwen.serendipity.discovery import DiscoveryResult, build_discovery
from spider_qwen.tools.fetch_service import MockFetchProvider
from spider_qwen.tools.search_service import MockSearchProvider


class _Ledger:
    """Minimal ledger stub exposing items()/get() over EvidenceItems."""

    def __init__(self, items):
        self._items = {it.ledger_id: it for it in items}

    def items(self):
        return list(self._items.values())

    def get(self, ledger_id):
        return self._items.get(ledger_id)


def _item(url, text, reliability=0.9, source_tool="mock"):
    return EvidenceItem(source_tool=source_tool, url=url, text=text, reliability=reliability)


# --- unit: build_discovery over a controlled ledger ------------------------

def test_three_slots_and_within_budget_even_when_empty():
    res = build_discovery("NE5532 substitute", _Ledger([]))
    assert isinstance(res, DiscoveryResult)
    assert [s.slot for s in res.slots] == ["S1", "S2", "S3"]
    assert res.within_budget is True


def test_s1_substitutes_from_graph_with_evidence():
    led = _Ledger([_item(
        "https://datasheet.example/ne5532",
        "NE5532 is superseded by NE5532A. NE5532 cross-references LM358. "
        "NE5532 is pin-compatible with TL072.",
    )])
    res = build_discovery("NE5532 substitute", led)
    s1 = next(s for s in res.slots if s.slot == "S1")
    assert s1.supported
    names = {it.detail.get("mpn") for it in s1.items}
    assert {"NE5532A", "LM358", "TL072"} <= names
    assert all(it.evidence_refs for it in s1.items)
    assert all(it.source_component == "graph_ppr" for it in s1.items)


def test_obsolete_substitute_is_gated_out_by_cove():
    led = _Ledger([_item(
        "https://datasheet.example/ne5532",
        "NE5532 is superseded by NE5532A. NE5532A is obsolete. NE5532 cross-references LM358.",
    )])
    res = build_discovery("NE5532 substitute", led)
    s1 = next(s for s in res.slots if s.slot == "S1")
    names = {it.detail.get("mpn") for it in s1.items}
    assert "LM358" in names
    assert "NE5532A" not in names  # dropped: the substitute is itself obsolete
    assert s1.note  # the removal is noted, not silent


def test_multi_hop_substitute_surfaced_via_ppr():
    # LM4562 is reachable only as NE5532 -> NE5532A -> LM4562 (2 hops); a direct-
    # neighbour-only S1 would drop it.
    led = _Ledger([_item(
        "https://datasheet.example/ne5532",
        "NE5532 is superseded by NE5532A. NE5532A cross-references LM4562.",
    )])
    res = build_discovery("NE5532 substitute", led)
    s1 = next(s for s in res.slots if s.slot == "S1")
    by_mpn = {it.detail["mpn"]: it for it in s1.items}
    assert "NE5532A" in by_mpn and by_mpn["NE5532A"].detail["depth"] == 1
    assert "LM4562" in by_mpn and by_mpn["LM4562"].detail["depth"] == 2
    assert by_mpn["LM4562"].evidence_refs
    assert "->" in by_mpn["LM4562"].detail["path"]  # human-readable relation path


def test_obsolete_original_in_one_sentence_does_not_taint_substitute():
    # "Obsolete NE5532 is superseded by NE5532A" -- the EOL marker is about NE5532,
    # the predecessor; the healthy successor NE5532A must NOT inherit it and be
    # dropped by CoVe.
    led = _Ledger([_item(
        "https://datasheet.example/ne5532",
        "Obsolete NE5532 is superseded by NE5532A.",
    )])
    res = build_discovery("NE5532 substitute", led)
    s1 = next(s for s in res.slots if s.slot == "S1")
    by_mpn = {it.detail["mpn"]: it for it in s1.items}
    assert "NE5532A" in by_mpn
    assert by_mpn["NE5532A"].detail["lifecycle"] != "eol"


def test_s2_surfaces_long_tail_broker_source():
    led = _Ledger([
        _item("https://datasheet.example/ne5532", "NE5532 is superseded by NE5532A."),
        _item("https://rochester-electronics.example/ne5532", "Obsolete NE5532 available.", reliability=0.5),
    ])
    res = build_discovery("NE5532 substitute", led)
    s2 = next(s for s in res.slots if s.slot == "S2")
    assert s2.supported
    assert any("rochester" in it.detail.get("host", "") for it in s2.items)
    assert all(it.evidence_refs for it in s2.items)


def test_s3_risk_and_dmsms_on_eol():
    led = _Ledger([_item(
        "https://datasheet.example/ne5532",
        "NE5532 is end-of-life (EOL). PCN-2024-001 has been issued. NE5532 is superseded by NE5532A.",
    )])
    res = build_discovery("NE5532 substitute", led)
    s3 = next(s for s in res.slots if s.slot == "S3")
    assert s3.supported
    assert any(it.kind == "dmsms" for it in s3.items)
    assert all(it.evidence_refs for it in s3.items)
    assert any("eol" in it.detail.get("lifecycle", "") for it in s3.items)


def test_unsupported_slots_marked_with_note():
    led = _Ledger([_item(
        "https://vendor.example/cleaning",
        "We provide office cleaning in Singapore. Email sales@vendor.example.",
        reliability=0.6,
    )])
    res = build_discovery("office cleaning Singapore", led)
    s1 = next(s for s in res.slots if s.slot == "S1")
    assert not s1.supported and s1.note
    assert res.within_budget is True


# --- integration: controller + CLI ----------------------------------------

def _controller():
    return Controller(search_provider=MockSearchProvider(), fetch_provider=MockFetchProvider(),
                      state_dir=None, persist=False)


def test_default_run_has_no_discovery():
    res = asyncio.run(_controller().run("NE5532 obsolete substitute replacement", mode="auto"))
    assert res.serendipity_discovery is None


def test_serendipity_run_populates_discovery_offline():
    res = asyncio.run(_controller().run(
        "NE5532 obsolete substitute replacement", mode="auto", serendipity=True))
    d = res.serendipity_discovery
    assert d is not None
    assert {s["slot"] for s in d["slots"]} == {"S1", "S2", "S3"}
    supported = [s for s in d["slots"] if s["supported"]]
    assert supported  # at least one sense proven from real components offline
    for s in supported:
        for it in s["items"]:
            assert it["evidence_refs"]
            assert it["source_component"]
    assert d["within_budget"] is True


def test_cli_serendipity_flag(capsys):
    rc = main(["run", "NE5532 obsolete substitute replacement", "--offline", "--serendipity"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["serendipity_discovery"] is not None

    rc = main(["run", "NE5532 obsolete substitute replacement", "--offline"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["serendipity_discovery"] is None
