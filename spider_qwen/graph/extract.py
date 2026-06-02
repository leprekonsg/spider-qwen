"""T-3.1: triple extraction for the supplier-part graph (REBEL/SAC-KG shape).

Generator -> Verifier -> Pruner, deterministic and offline:

- Generator: per sentence, locate a relation phrase, then the nearest entity on
  its left (subject) and right (object). Entities are MPN-like tokens (Part) or
  known manufacturer/distributor names; everything else is ignored.
- Verifier: each triple's object surface must be grounded in the source text
  (MiniCheck, the T-2.2 spine), so a hallucinated relation cannot be upserted.
- Pruner: drop self-loops and duplicate (subject, rel, object) triples.

Verified triples are upserted into the ``GraphStore``; every edge carries the
asserting claim's ``evidence_claim_id`` (a ledger_id), never a bare URL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..verification.minicheck import MiniCheck
from .schema import dist_key, mfr_key, part_key

# Relation phrase -> edge type. Order matters: longer/more-specific phrases first.
_REL_PHRASES: tuple[tuple[str, str], ...] = (
    ("cross-references", "CROSS_REFERENCE"),
    ("cross references", "CROSS_REFERENCE"),
    ("cross-reference for", "CROSS_REFERENCE"),
    ("is superseded by", "SUPERSEDED_BY"),
    ("superseded by", "SUPERSEDED_BY"),
    ("replaced by", "SUPERSEDED_BY"),
    ("pin-compatible with", "PIN_COMPATIBLE_WITH"),
    ("pin compatible with", "PIN_COMPATIBLE_WITH"),
    ("same die as", "SAME_DIE_AS"),
    ("renamed to", "RENAMED_TO"),
    ("was acquired by", "ACQUIRED_BY"),
    ("acquired by", "ACQUIRED_BY"),
    ("affected by", "AFFECTED_BY"),
    ("franchise for", "FRANCHISE_FOR"),
    ("authorized distributor for", "FRANCHISE_FOR"),
    ("is manufactured by", "MANUFACTURED_BY"),
    ("manufactured by", "MANUFACTURED_BY"),
    ("stocked at", "STOCKED_AT"),
)

# Known entity names (surface -> canonical key). Distinct from page_judge's
# *domain* lists: these are company names that appear in datasheet prose.
_MANUFACTURERS = {
    "atmel": mfr_key("atmel"),
    "microchip technology": mfr_key("microchip"),
    "microchip": mfr_key("microchip"),
    "texas instruments": mfr_key("ti"),
    "stmicroelectronics": mfr_key("st"),
    "analog devices": mfr_key("analog"),
    "nxp": mfr_key("nxp"),
    "infineon": mfr_key("infineon"),
    "onsemi": mfr_key("onsemi"),
    "renesas": mfr_key("renesas"),
}
_DISTRIBUTORS = {
    "digikey": dist_key("digikey"),
    "digi-key": dist_key("digikey"),
    "mouser": dist_key("mouser"),
    "arrow": dist_key("arrow"),
    "farnell": dist_key("farnell"),
}

_MPN_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]{2,}\b")
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class Triple:
    subject_id: str
    subject_type: str
    subject_surface: str
    rel: str
    object_id: str
    object_type: str
    object_surface: str
    confidence: float
    sentence: str


def _is_mpn(token: str) -> bool:
    # A manufacturer part number has letters and at least one digit (>=4 chars):
    # "ATMEGA48A" yes, "Microchip" no, "the" no.
    return len(token) >= 4 and any(c.isalpha() for c in token) and any(c.isdigit() for c in token)


def _resolve(surface: str) -> tuple[str, str] | None:
    """Return (node_type, node_id) for a known entity surface, else None."""
    key = _WS.sub(" ", surface.strip().lower())
    if key in _MANUFACTURERS:
        return "Manufacturer", _MANUFACTURERS[key]
    if key in _DISTRIBUTORS:
        return "Distributor", _DISTRIBUTORS[key]
    if _is_mpn(surface.strip()):
        return "Part", part_key(surface.strip())
    return None


def _entities(text: str) -> list[tuple[int, str, str, str]]:
    """All resolvable entities in ``text`` as (start, type, id, surface), in order."""
    found: list[tuple[int, str, str, str]] = []
    low = text.lower()
    for name in (*_MANUFACTURERS, *_DISTRIBUTORS):
        start = 0
        while True:
            i = low.find(name, start)
            if i < 0:
                break
            # word-boundary check so "armicrochip" doesn't match "microchip"
            before_ok = i == 0 or not low[i - 1].isalnum()
            after = i + len(name)
            after_ok = after >= len(low) or not low[after].isalnum()
            if before_ok and after_ok:
                resolved = _resolve(name)
                if resolved:
                    found.append((i, resolved[0], resolved[1], name))
            start = i + len(name)
    for m in _MPN_RE.finditer(text):
        if _is_mpn(m.group(0)):
            found.append((m.start(), "Part", part_key(m.group(0)), m.group(0)))
    # Prefer the longest match at a given position (drop a Part subsumed by a name).
    found.sort(key=lambda e: (e[0], -len(e[3])))
    return found


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"[.!?\n]+", text or "") if s.strip()]


def extract_triples(text: str, *, confidence: float = 0.7) -> list[Triple]:
    """Generator + Pruner: candidate triples, deduped, no self-loops."""
    triples: list[Triple] = []
    seen: set[tuple[str, str, str]] = set()
    for sentence in _sentences(text):
        low = sentence.lower()
        for phrase, rel in _REL_PHRASES:
            idx = low.find(phrase)
            if idx < 0:
                continue
            left = [e for e in _entities(sentence[:idx])]
            right = [e for e in _entities(sentence[idx + len(phrase):])]
            if not left or not right:
                continue
            subj = left[-1]
            obj = right[0]
            if subj[2] == obj[2]:
                continue  # self-loop
            dedupe_key = (subj[2], rel, obj[2])
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            triples.append(Triple(
                subject_id=subj[2], subject_type=subj[1], subject_surface=subj[3],
                rel=rel, object_id=obj[2], object_type=obj[1], object_surface=obj[3],
                confidence=confidence, sentence=sentence,
            ))
            break  # one relation per sentence
    return triples


def ingest_text(
    store,
    text: str,
    *,
    evidence_claim_id: str,
    reliability: float = 1.0,
    minicheck: MiniCheck | None = None,
    confidence: float = 0.7,
) -> list[Triple]:
    """Generator -> Verifier (MiniCheck) -> Pruner -> upsert into the store.

    ``evidence_claim_id`` is the ledger_id of the page that asserts these triples;
    every edge points back at it (hard rule: never a bare URL).
    """
    mc = minicheck or MiniCheck()
    added: list[Triple] = []
    for tr in extract_triples(text, confidence=confidence):
        verdict = mc.check(
            claim=f"{tr.subject_surface} {tr.rel} {tr.object_surface}",
            value=tr.object_surface, evidence_span=text,
        )
        if not verdict.supported:
            continue  # ungrounded relation -> never upserted
        store.upsert_node(tr.subject_id, tr.subject_type, {"surface": tr.subject_surface})
        store.upsert_node(tr.object_id, tr.object_type, {"surface": tr.object_surface})
        store.add_edge(
            tr.subject_id, tr.object_id, tr.rel,
            confidence=tr.confidence, reliability=reliability,
            evidence_claim_id=evidence_claim_id,
        )
        added.append(tr)
    return added
