"""Tests for vendor-name legal-suffix normalization and candidate deduplication.

Covers the bug from run_9fbe2f94d8d1 where "ORIGIN Exterminators Pte. Ltd."
and "ORIGIN Exterminators" were treated as two distinct candidates.
"""

from __future__ import annotations

import pytest

from spider_qwen.extraction.dedupe import normalize_vendor_name, dedupe_candidates
from spider_qwen.evidence.models import EvidenceRef
from spider_qwen.modes.contracts import (
    QuoteChannel,
    QuoteChannelType,
    ServiceCandidate,
)


# ---------------------------------------------------------------------------
# normalize_vendor_name unit tests
# ---------------------------------------------------------------------------

def test_normalize_strips_pte_ltd_with_dots():
    assert normalize_vendor_name("ORIGIN Exterminators Pte. Ltd.") == "origin exterminators"


def test_normalize_strips_bare_suffix():
    assert normalize_vendor_name("ORIGIN Exterminators") == "origin exterminators"


def test_normalize_same_key_for_both_variants():
    a = normalize_vendor_name("ORIGIN Exterminators Pte. Ltd.")
    b = normalize_vendor_name("ORIGIN Exterminators")
    assert a == b, f"Expected same key, got {a!r} vs {b!r}"


def test_normalize_anticimex_variants():
    a = normalize_vendor_name("Anticimex Pte Ltd")
    b = normalize_vendor_name("Anticimex")
    assert a == b == "anticimex"


def test_normalize_rentokil_initial_not_merged_with_rentokil():
    a = normalize_vendor_name("Rentokil Initial Singapore")
    b = normalize_vendor_name("Rentokil")
    assert a != b, f"Should NOT collapse: {a!r} vs {b!r}"


def test_normalize_suffix_only_name_returns_empty():
    assert normalize_vendor_name("Pte Ltd") == ""
    assert normalize_vendor_name("Limited") == ""
    assert normalize_vendor_name("LLC") == ""


def test_normalize_empty_or_whitespace():
    assert normalize_vendor_name("") == ""
    assert normalize_vendor_name("   ") == ""


def test_normalize_unicode_punctuation_does_not_crash():
    # Accented chars become spaces after regex; the remaining tokens are kept.
    result = normalize_vendor_name("Kärcher Pte. Ltd.")
    assert "ltd" not in result
    assert "pte" not in result
    # "k rcher" after stripping non-ascii; must not raise
    assert isinstance(result, str)


def test_normalize_strips_multiple_trailing_suffixes():
    # "Sdn Bhd" is a two-token suffix sequence.
    assert normalize_vendor_name("Rentokil Sdn Bhd") == "rentokil"


def test_normalize_does_not_strip_middle_tokens():
    # "Inc" in the middle of a name must not be stripped.
    result = normalize_vendor_name("Incorporated Solutions Singapore")
    assert "solutions" in result
    assert "singapore" in result


def test_normalize_plc_suffix():
    assert normalize_vendor_name("G4S Plc") == "g4s"


def test_normalize_llp_suffix():
    assert normalize_vendor_name("Deloitte LLP") == "deloitte"


def test_normalize_corp_suffix():
    assert normalize_vendor_name("PestAway Corp") == "pestaway"


# ---------------------------------------------------------------------------
# Helpers for candidate construction
# ---------------------------------------------------------------------------

def _ref(n: int = 1) -> EvidenceRef:
    return EvidenceRef(
        ledger_id=f"ev_{n}", url=f"https://vendor{n}.sg",
        snippet_hash="h", retrieved_at="2026-01-01T00:00:00Z",
    )


def _svc(
    vendor_name: str,
    *,
    website: str | None = None,
    n_refs: int = 1,
) -> ServiceCandidate:
    refs = [_ref(i) for i in range(n_refs)]
    qc = QuoteChannel(
        type=QuoteChannelType.CONTACT_EMAIL,
        value="info@vendor.sg",
        evidence_ref=refs[0],
    )
    return ServiceCandidate(
        vendor_name=vendor_name,
        website=website,
        country="Singapore",
        service_match_score=1.0,
        service_match_evidence=True,
        quote_channel=qc,
        evidence_refs=refs,
    )


# ---------------------------------------------------------------------------
# dedupe_candidates integration tests
# ---------------------------------------------------------------------------

def test_origin_variants_merge_returns_one():
    long = _svc("ORIGIN Exterminators Pte. Ltd.", n_refs=2)
    short = _svc("ORIGIN Exterminators", n_refs=1)
    result, merges = dedupe_candidates([long, short])
    assert len(result) == 1, "Expected merge to 1 candidate"
    assert merges == 1


def test_origin_richer_candidate_kept():
    """The richer candidate (more evidence_refs) wins, not insertion order."""
    thin = _svc("ORIGIN Exterminators", n_refs=1)
    rich = _svc("ORIGIN Exterminators Pte. Ltd.", n_refs=3)
    # Insert thin first so a keeps-first policy would wrongly keep thin.
    result, merges = dedupe_candidates([thin, rich])
    assert len(result) == 1
    assert result[0].vendor_name == "ORIGIN Exterminators Pte. Ltd."
    assert merges == 1


def test_anticimex_variants_merge():
    a = _svc("Anticimex Pte Ltd", n_refs=2)
    b = _svc("Anticimex", n_refs=1)
    result, merges = dedupe_candidates([a, b])
    assert len(result) == 1
    assert merges == 1


def test_rentokil_initial_vs_rentokil_not_merged():
    """Different remaining tokens: must stay separate."""
    a = _svc("Rentokil Initial Singapore")
    b = _svc("Rentokil")
    result, merges = dedupe_candidates([a, b])
    assert len(result) == 2, "Rentokil Initial Singapore and Rentokil must NOT merge"
    assert merges == 0


def test_suffix_only_names_stay_distinct():
    """Two suffix-only names must not collapse into the same empty key."""
    a = _svc("Pte Ltd")
    b = _svc("Limited")
    result, merges = dedupe_candidates([a, b])
    assert len(result) == 2, "Suffix-only names must remain distinct"


def test_no_merge_on_empty_normalized_key():
    """A name that normalizes to empty must not silently merge with another."""
    a = _svc("Pte Ltd")
    b = _svc("LLC")
    result, merges = dedupe_candidates([a, b])
    assert len(result) == 2


def test_domain_key_takes_precedence_over_name():
    """If both candidates share a domain, they collapse via domain key
    regardless of name differences — existing behaviour preserved."""
    a = _svc("Vendor Alpha Pte Ltd", website="https://vendor.sg")
    b = _svc("Vendor Alpha", website="https://vendor.sg")
    result, merges = dedupe_candidates([a, b])
    assert len(result) == 1


def test_no_false_merge_different_companies():
    a = _svc("PestAway Singapore")
    b = _svc("GreenSweep Singapore")
    result, merges = dedupe_candidates([a, b])
    assert len(result) == 2
    assert merges == 0


def test_unicode_odd_punctuation_does_not_crash():
    a = _svc("Kärcher Pte. Ltd.")
    result, merges = dedupe_candidates([a])
    assert len(result) == 1


def test_tie_break_longer_name_wins():
    """Equal evidence_refs -> longer vendor_name is kept."""
    short = _svc("Anticimex", n_refs=2)
    long = _svc("Anticimex Pte Ltd", n_refs=2)
    result, merges = dedupe_candidates([short, long])
    assert len(result) == 1
    assert result[0].vendor_name == "Anticimex Pte Ltd"
    assert merges == 1


def test_merge_count_reflects_all_collapses():
    a = _svc("PestBusters Pte Ltd", n_refs=3)
    b = _svc("PestBusters", n_refs=1)
    c = _svc("PestBusters Pte. Ltd.", n_refs=2)
    result, merges = dedupe_candidates([a, b, c])
    assert len(result) == 1
    assert merges == 2


def test_dedupe_empty_list():
    result, merges = dedupe_candidates([])
    assert result == []
    assert merges == 0
