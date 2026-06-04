"""T-2.3: Chain-of-Verification (CoVe) + semantic entropy for substitutes.

Deterministic and offline by default, matching spider-qwen's hot path:

- ``ChainOfVerification`` wraps a draft set of substitute suggestions, asks a
  fixed set of verification questions per candidate, answers each against the
  candidate's evidence-grounded attributes (lifecycle, presence of evidence) --
  not the draft's self-assertion -- and revises by dropping any candidate that
  fails a critical question. So a plausible-but-discontinued ("phantom")
  substitute is removed even if the draft claimed high confidence.
- ``semantic_entropy`` scores answer confidence by clustering N samples on
  (MPN, manufacturer): agreement -> low entropy -> high confidence; scatter ->
  high entropy -> low confidence.

An optional ``sampler`` seam draws the N samples per candidate (e.g. a Qwen
generator). Without one, CoVe keeps each candidate's own ``confidence`` and only
applies the deterministic verification questions.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any, Callable

from pydantic import BaseModel, Field

from .. import SCHEMA_VERSION
from ..evidence.models import EvidenceRef

# Lifecycle states (normalized) that disqualify a substitute outright. NRND is
# excluded: it is derated, not dead, and is a meaningful side of a dispute (T-2.3
# contradiction detection), so CoVe keeps it.
DEAD_STATUSES = frozenset({"obsolete", "eol", "endoflife", "discontinued", "inactive"})

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _norm(text: str) -> str:
    return _NON_ALNUM.sub("", (text or "").lower())


class SubstituteCandidate(BaseModel):
    """A drafted part substitute, evidence-backed."""

    schema_version: str = SCHEMA_VERSION
    mpn: str
    manufacturer: str = ""
    lifecycle_status: str = "unknown"
    confidence: float = 0.0
    rationale: str = ""
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)

    def cluster_key(self) -> str:
        return f"{_norm(self.mpn)}|{_norm(self.manufacturer)}"


class VerificationCheck(BaseModel):
    mpn: str
    question: str
    passed: bool
    rationale: str = ""


class EntropyResult(BaseModel):
    entropy: float
    confidence: float
    clusters: dict[str, int] = Field(default_factory=dict)
    top_cluster_fraction: float = 0.0


class CoVeResult(BaseModel):
    verified: list[SubstituteCandidate] = Field(default_factory=list)
    removed: list[SubstituteCandidate] = Field(default_factory=list)
    checks: list[VerificationCheck] = Field(default_factory=list)


def _sample_key(sample: Any) -> str:
    if isinstance(sample, SubstituteCandidate):
        return sample.cluster_key()
    if isinstance(sample, dict):
        return f"{_norm(sample.get('mpn', ''))}|{_norm(sample.get('manufacturer', ''))}"
    return _norm(str(sample))


def semantic_entropy(samples: list[Any]) -> EntropyResult:
    """Confidence from cluster agreement over samples keyed on (MPN, manufacturer).

    Confidence is ``1 - normalized_entropy``: a single cluster -> 1.0, a uniform
    scatter over k clusters -> ~0.0.
    """
    keys = [k for k in (_sample_key(s) for s in samples) if k.strip("|")]
    n = len(keys)
    if n == 0:
        return EntropyResult(entropy=0.0, confidence=0.0)
    counts = Counter(keys)
    probs = [c / n for c in counts.values()]
    entropy = -sum(p * math.log(p) for p in probs)
    max_entropy = math.log(len(counts)) if len(counts) > 1 else 0.0
    norm_entropy = entropy / max_entropy if max_entropy > 0 else 0.0
    return EntropyResult(
        entropy=round(entropy, 4),
        confidence=round(1.0 - norm_entropy, 4),
        clusters=dict(counts),
        top_cluster_fraction=round(max(counts.values()) / n, 4),
    )


class ChainOfVerification:
    """Draft -> verify-questions -> answer-independently -> revise."""

    def __init__(
        self,
        *,
        sampler: Callable[[SubstituteCandidate], list[Any]] | None = None,
        min_confidence: float = 0.0,
    ) -> None:
        self.sampler = sampler
        self.min_confidence = float(min_confidence)

    def verify(self, drafts: list[SubstituteCandidate]) -> CoVeResult:
        result = CoVeResult()
        for cand in drafts:
            checks = self._questions(cand)
            passed = all(c.passed for c in checks)
            if passed and self.sampler is not None:
                ent = semantic_entropy(self.sampler(cand))
                cand = cand.model_copy(update={"confidence": ent.confidence})
                agree = ent.confidence >= self.min_confidence
                checks.append(VerificationCheck(
                    mpn=cand.mpn,
                    question="Do independent samples agree on (MPN, manufacturer)?",
                    passed=agree,
                    rationale=f"semantic-entropy confidence {ent.confidence} vs min {self.min_confidence}",
                ))
                passed = agree
            result.checks.extend(checks)
            (result.verified if passed else result.removed).append(cand)
        return result

    @staticmethod
    def _questions(cand: SubstituteCandidate) -> list[VerificationCheck]:
        in_production = _norm(cand.lifecycle_status) not in DEAD_STATUSES
        has_evidence = bool(cand.evidence_refs)
        return [
            VerificationCheck(
                mpn=cand.mpn, question="Is the part still in production (not EOL/obsolete)?",
                passed=in_production,
                rationale=f"lifecycle_status={cand.lifecycle_status or 'unknown'}",
            ),
            VerificationCheck(
                mpn=cand.mpn, question="Is the substitute backed by evidence?",
                passed=has_evidence,
                rationale=f"{len(cand.evidence_refs)} evidence ref(s)",
            ),
        ]
