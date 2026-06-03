"""T-5.1: legacy cross-reference OCR miner.

Old replacement guides (NTE, ECG-Master, Motorola->Fairchild,
Philips-Signetics) are TABLES of original-part -> replacement-part rows, not
prose -- so the T-3.1 relation-phrase extractor does not apply. This miner parses
the tabular rows into MPN->MPN ``CROSS_REFERENCE`` edges and upserts them into the
supplier-part graph with ``props={"source": "legacy_book"}``. Serves S1: a
substitute graph mined from 40-year-old replacement books.

Deterministic + offline. The OCR step (Qwen-VL-OCR in production) is an injected
``ocr_fn`` seam; offline callers pass already-extracted text. Every edge points at
the OCR'd page's ledger entry (``evidence_claim_id``), never a bare reference.

"Validated" here is structural: each surface must be MPN-shaped (a letter and a
digit, plausible length), the pair must co-occur on one row, self-loops are
dropped, and duplicates collapse. Grounding is inherent -- both MPNs are read
directly from the source row -- so no separate entailment pass is needed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from ..graph.schema import part_key

# Legacy MPNs may lead with a digit (2N2222, 1N4148) and carry hyphens/dots/slashes
# (DF13-6P-1.25DSA). The character class spans those so a full part is one token.
_LEGACY_MPN_RE = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9./-]{2,}\b")

# OCR'd pages carry document/version references that happen to have a letter and a
# digit (Rev.2.1, Fig.1.23, Page12, Section3). Reject a token whose leading letters
# are one of these words. Deliberately excludes ambiguous prefixes that ARE real
# part families (e.g. "ref" -> TI REF200), trading a little recall for no junk edges.
_NON_MPN_WORDS = frozenset({
    "fig", "figure", "page", "section", "table", "note", "notes", "rev",
    "revision", "version", "chapter", "volume", "appendix", "para", "paragraph",
})
_LEADING_ALPHA_RE = re.compile(r"[A-Za-z]+")

OcrFn = Callable[[object], str]


@dataclass(frozen=True)
class CrossRef:
    original: str
    replacement: str
    original_id: str
    replacement_id: str
    line: str


def _is_mpn(token: str) -> bool:
    core = token.strip()
    if not 4 <= len(core) <= 24:
        return False
    if not (any(c.isalpha() for c in core) and any(c.isdigit() for c in core)):
        return False
    lead = _LEADING_ALPHA_RE.match(core)
    if lead and lead.group(0).lower() in _NON_MPN_WORDS:
        return False  # document/version reference, not a part number
    return True


def parse_cross_refs(text: str) -> list[CrossRef]:
    """Validated original->replacement pairs, one row at a time.

    The first MPN on a row is the original; every later MPN on that row is a
    replacement (handles "2N3904  NTE123" and "2N3904: NTE123, NTE123A").
    """
    refs: list[CrossRef] = []
    seen: set[tuple[str, str]] = set()
    for line in (text or "").splitlines():
        mpns = [m.group(0) for m in _LEGACY_MPN_RE.finditer(line) if _is_mpn(m.group(0))]
        if len(mpns) < 2:
            continue
        original = mpns[0]
        oid = part_key(original)
        for repl in mpns[1:]:
            rid = part_key(repl)
            if rid == oid:
                continue  # self-loop
            key = (oid, rid)
            if key in seen:
                continue  # duplicate pair across rows
            seen.add(key)
            refs.append(CrossRef(original, repl, oid, rid, line.strip()))
    return refs


def ingest_legacy_text(
    store,
    text: str,
    *,
    evidence_claim_id: str,
    reliability: float = 0.85,
    confidence: float = 0.7,
    source: str = "legacy_book",
) -> list[CrossRef]:
    """Parse ``text`` and upsert each pair as a CROSS_REFERENCE edge.

    ``evidence_claim_id`` is the ledger_id of the OCR'd page; every edge points
    back at it (hard rule: never a bare reference).
    """
    added: list[CrossRef] = []
    for ref in parse_cross_refs(text):
        store.upsert_node(ref.original_id, "Part", {"surface": ref.original})
        store.upsert_node(ref.replacement_id, "Part", {"surface": ref.replacement})
        store.add_edge(
            ref.original_id, ref.replacement_id, "CROSS_REFERENCE",
            confidence=confidence, reliability=reliability,
            evidence_claim_id=evidence_claim_id, props={"source": source},
        )
        added.append(ref)
    return added


def mine_legacy_page(
    store, image: object, ocr_fn: OcrFn, *, evidence_claim_id: str, **kw
) -> list[CrossRef]:
    """OCR a scanned page via the injected ``ocr_fn``, then ingest its cross-refs."""
    return ingest_legacy_text(store, ocr_fn(image), evidence_claim_id=evidence_claim_id, **kw)
