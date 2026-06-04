"""T-5.1: legacy cross-reference OCR miner.

A scanned replacement guide (NTE / ECG-Master / Motorola->Fairchild /
Philips-Signetics) is a TABLE of original-part -> replacement-part rows. The
miner parses those rows into MPN->MPN CROSS_REFERENCE edges in the supplier-part
graph, tagged ``source="legacy_book"`` and tied to the OCR'd page's ledger entry.
OCR is an injected seam, so these tests are deterministic and offline.
"""

from __future__ import annotations

from spider_qwen.evidence.ledger import EvidenceLedger
from spider_qwen.graph.store import GraphStore
from spider_qwen.serendipity.legacy_ocr_miner import (
    ingest_legacy_text,
    mine_legacy_page,
    parse_cross_refs,
)

# A realistic NTE-style cross-reference page (14 data rows + noise lines).
LEGACY_PAGE = """\
NTE Semiconductors Cross-Reference Guide (1998 ed.)
Original        Replacement
2N2222          NTE123A
2N2222A         NTE123AP
2N3904          NTE123
2N3906          NTE159
2N4401          NTE123AP
2N4403          NTE159
BC547           NTE123AP
BC557           NTE159
1N4148          NTE519
1N4001          NTE116
1N4007          NTE116
TIP31C          NTE291
TIP32C          NTE292
2SC1815         NTE85
Page 12
"""


def _record_page(ledger: EvidenceLedger) -> str:
    ref = ledger.record(
        source_tool="legacy_ocr",
        url="legacy://nte-cross-ref-1998/p12",
        snippet=LEGACY_PAGE[:200],
        text=LEGACY_PAGE,
    )
    return ref.ledger_id


def test_parse_cross_refs_extracts_pairs():
    refs = parse_cross_refs(LEGACY_PAGE)
    assert len(refs) == 14  # one per data row; header/title/page lines excluded
    pairs = {(r.original, r.replacement) for r in refs}
    assert ("2N3904", "NTE123") in pairs
    assert ("2SC1815", "NTE85") in pairs


def test_parse_ignores_headers_and_page_numbers():
    refs = parse_cross_refs(LEGACY_PAGE)
    surfaces = {r.original for r in refs} | {r.replacement for r in refs}
    # Every surface is an MPN (has a letter and a digit); no header words / years.
    assert all(any(c.isalpha() for c in s) and any(c.isdigit() for c in s) for s in surfaces)
    assert "Original" not in surfaces and "Page" not in surfaces and "1998" not in surfaces


def test_ingest_builds_validated_cross_reference_edges():
    store = GraphStore()
    ledger = EvidenceLedger("run_test", None)
    claim_id = _record_page(ledger)

    added = ingest_legacy_text(store, LEGACY_PAGE, evidence_claim_id=claim_id)
    assert len(added) >= 10
    assert store.edge_count() >= 10

    edges = store.edges()
    assert edges and all(e["rel"] == "CROSS_REFERENCE" for e in edges)

    # Acceptance: the original->replacement edge is reachable in the graph.
    hop = store.traverse("part:2n3904", rels=["CROSS_REFERENCE"], max_depth=1)
    assert any(h["id"] == "part:nte123" for h in hop)


def test_edges_carry_legacy_source_and_resolvable_evidence():
    store = GraphStore()
    ledger = EvidenceLedger("run_test", None)
    claim_id = _record_page(ledger)
    ingest_legacy_text(store, LEGACY_PAGE, evidence_claim_id=claim_id)

    for e in store.edges():
        assert e["evidence_claim_id"] == claim_id
        assert ledger.get(e["evidence_claim_id"]) is not None  # evidence or it didn't happen
    # props.source is on the full row view.
    full = store.current_edges()
    assert full and all(r["props"].get("source") == "legacy_book" for r in full)


def test_mine_legacy_page_uses_ocr_seam():
    store = GraphStore()
    ledger = EvidenceLedger("run_test", None)
    claim_id = _record_page(ledger)
    calls: list[object] = []

    def fake_ocr(image: object) -> str:
        calls.append(image)
        return LEGACY_PAGE

    added = mine_legacy_page(store, b"<scanned page bytes>", fake_ocr, evidence_claim_id=claim_id)
    assert calls == [b"<scanned page bytes>"]  # OCR seam invoked once
    assert len(added) == 14
    assert store.edge_count() == 14


def test_document_references_are_not_mined_as_parts():
    # OCR noise (Rev/Fig/Page/Section) carries a letter+digit but is not a part.
    # A contaminated row must still yield only the real original->replacement edge.
    store = GraphStore()
    text = (
        "2N3904 NTE123 Rev.2.1\n"
        "1N4148 NTE519 (See Fig.1.23)\n"
        "Section3 Page12\n"
    )
    added = ingest_legacy_text(store, text, evidence_claim_id="ev1")
    pairs = {(r.original, r.replacement) for r in added}
    assert pairs == {("2N3904", "NTE123"), ("1N4148", "NTE519")}
    dsts = {e["dst"] for e in store.edges()}
    assert "part:rev21" not in dsts and "part:fig123" not in dsts


def test_no_self_loop_or_duplicate_edges():
    store = GraphStore()
    text = "2N3904 NTE123\n2N3904 NTE123\nBC547 BC547\n"  # dup row + a self-loop row
    added = ingest_legacy_text(store, text, evidence_claim_id="ev1")
    assert len(added) == 1  # dup collapsed, self-loop dropped
    assert added[0].original == "2N3904" and added[0].replacement == "NTE123"
