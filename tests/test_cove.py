"""T-2.3: Chain-of-Verification (CoVe) + semantic entropy for substitutes.

Deterministic + offline: verification questions are answered against each
candidate's evidence-grounded attributes (lifecycle/evidence), never the draft's
self-assertion; semantic entropy clusters samples on (MPN, manufacturer).
Acceptance: a phantom (plausible-but-discontinued) substitute is removed by CoVe.
"""

from __future__ import annotations

from spider_qwen.evidence.models import EvidenceRef, utc_now_iso
from spider_qwen.verification.cove import (
    ChainOfVerification,
    SubstituteCandidate,
    semantic_entropy,
)


def _ref(lid: str = "ev_x") -> EvidenceRef:
    return EvidenceRef(ledger_id=lid, url=f"https://{lid}.example",
                       snippet_hash="h", retrieved_at=utc_now_iso())


def _sub(mpn: str, mfr: str = "Microchip", lifecycle: str = "active",
         refs: list | None = None, confidence: float = 0.0) -> SubstituteCandidate:
    return SubstituteCandidate(
        mpn=mpn, manufacturer=mfr, lifecycle_status=lifecycle,
        confidence=confidence, evidence_refs=refs if refs is not None else [_ref()],
    )


# --- semantic entropy ------------------------------------------------------

def test_semantic_entropy_zero_when_all_samples_agree():
    samples = [{"mpn": "ATMEGA48A", "manufacturer": "Microchip"}] * 5
    r = semantic_entropy(samples)
    assert r.entropy == 0.0
    assert r.confidence == 1.0
    assert len(r.clusters) == 1
    assert r.top_cluster_fraction == 1.0


def test_semantic_entropy_low_confidence_when_samples_all_differ():
    samples = [{"mpn": f"PART{i}", "manufacturer": f"M{i}"} for i in range(5)]
    r = semantic_entropy(samples)
    assert r.confidence < 0.3  # max entropy -> ~0 confidence
    assert len(r.clusters) == 5


def test_semantic_entropy_clusters_normalize_case_and_spacing():
    samples = [
        {"mpn": "ATmega48", "manufacturer": "Microchip"},
        {"mpn": "atmega 48", "manufacturer": "microchip"},
        {"mpn": "ATMEGA48", "manufacturer": "MICROCHIP"},
    ]
    r = semantic_entropy(samples)
    assert len(r.clusters) == 1
    assert r.confidence == 1.0


def test_semantic_entropy_majority_cluster_is_intermediate():
    samples = [
        {"mpn": "A", "manufacturer": "M"},
        {"mpn": "A", "manufacturer": "M"},
        {"mpn": "A", "manufacturer": "M"},
        {"mpn": "B", "manufacturer": "N"},
        {"mpn": "C", "manufacturer": "O"},
    ]
    r = semantic_entropy(samples)
    assert 0.0 < r.confidence < 1.0
    assert r.top_cluster_fraction == 0.6


def test_semantic_entropy_empty_is_zero_confidence():
    r = semantic_entropy([])
    assert r.confidence == 0.0
    assert r.clusters == {}


# --- Chain-of-Verification -------------------------------------------------

def test_cove_removes_discontinued_substitute():
    # Acceptance 1: a plausible-but-discontinued ("phantom") substitute is dropped.
    drafts = [
        _sub("ATMEGA48A", lifecycle="active"),
        _sub("ATMEGA48", lifecycle="obsolete"),
    ]
    result = ChainOfVerification().verify(drafts)
    kept = {s.mpn for s in result.verified}
    assert "ATMEGA48A" in kept
    assert "ATMEGA48" not in kept
    assert {s.mpn for s in result.removed} == {"ATMEGA48"}
    assert any(c.mpn == "ATMEGA48" and not c.passed for c in result.checks)


def test_cove_keeps_active_substitute_with_evidence():
    result = ChainOfVerification().verify([_sub("ATMEGA48A", lifecycle="active")])
    assert [s.mpn for s in result.verified] == ["ATMEGA48A"]


def test_cove_drops_substitute_without_evidence():
    result = ChainOfVerification().verify([_sub("ATMEGA48A", lifecycle="active", refs=[])])
    assert result.verified == []
    assert any(not c.passed and "evidence" in c.question.lower() for c in result.checks)


def test_cove_revision_is_independent_of_draft_confidence():
    # A self-asserted high confidence cannot save an EOL part: CoVe re-checks evidence.
    result = ChainOfVerification().verify(
        [_sub("ATMEGA48", lifecycle="eol", confidence=0.99)]
    )
    assert result.verified == []


def test_cove_sampler_low_agreement_drops_candidate():
    def sampler(_cand):
        return [{"mpn": f"P{i}", "manufacturer": f"M{i}"} for i in range(5)]

    result = ChainOfVerification(sampler=sampler, min_confidence=0.5).verify(
        [_sub("ATMEGA48A", lifecycle="active")]
    )
    assert result.verified == []


def test_cove_sampler_high_agreement_keeps_and_scores_candidate():
    def sampler(_cand):
        return [{"mpn": "ATMEGA48A", "manufacturer": "Microchip"}] * 5

    result = ChainOfVerification(sampler=sampler, min_confidence=0.5).verify(
        [_sub("ATMEGA48A", lifecycle="active")]
    )
    assert [s.mpn for s in result.verified] == ["ATMEGA48A"]
    assert result.verified[0].confidence == 1.0
