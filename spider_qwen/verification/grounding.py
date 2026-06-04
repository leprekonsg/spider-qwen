"""GSAR-style typed grounding: a four-way verdict instead of verified/unverified.

The binary MiniCheck verdict loses information the pipeline can act on. The
four-way partition distinguishes *why* a claim failed:

- ``grounded``       the cited span supports the claim          -> proceed
- ``complementary``  the cited span does not, but another ledger
                     span does (SAFE found it)                  -> regenerate
                     (re-cite; candidate for ledger insertion)
- ``contradicted``   the cited span asserts a *different* value
                     for the same subject/field                 -> replan
- ``ungrounded``     supported nowhere in the corpus            -> regenerate

``contradicted`` is the dispute-resolution driver: it means the source was
read and disagrees, which no amount of re-citing fixes - the plan itself
(query, source selection) is wrong. Deterministic, regex-only, no LLM.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from . import minicheck as _mc

GroundingLabel = Literal["grounded", "complementary", "contradicted", "ungrounded"]
Decision = Literal["proceed", "regenerate", "replan"]

_DECISION: dict[str, Decision] = {
    "grounded": "proceed",
    "complementary": "regenerate",
    "ungrounded": "regenerate",
    "contradicted": "replan",
}
# Severity order for aggregating a per-candidate decision.
_SEVERITY = {"proceed": 0, "regenerate": 1, "replan": 2}


class GroundingResult(BaseModel):
    label: GroundingLabel
    decision: Decision
    rationale: str = ""


def detect_numeric_contradiction(subject: str, value: str, premise: str) -> bool:
    """Does the cited span assert a *different* number for this subject?

    Only pure-numeric values (price, MOQ) can auto-contradict: a sentence
    grounding the subject that contains numbers, none of which equal the
    claimed value. Non-numeric values never auto-contradict (conservative:
    a different email on the page may simply be a second contact).
    """
    norm_value = _mc._norm(value)
    if not _mc._NUMERIC.match(norm_value):
        return False
    target = float(norm_value)
    tokens = _mc._subject_tokens(subject)
    found_other = False
    for sent in _mc._sentences(premise):
        if subject.strip():
            if tokens and not _mc._subject_in_sentence(tokens, sent):
                continue
            if not tokens and _mc._norm(subject) not in _mc._norm(sent):
                continue
        numbers = [float(n) for n in _mc._NUMBER_IN_TEXT.findall(_mc._norm(sent))]
        if not numbers:
            continue
        if any(abs(n - target) < 1e-9 for n in numbers):
            return False  # the claimed value is co-located after all
        found_other = True
    return found_other


def classify_grounding(
    *,
    supported_on_cited: bool,
    supported_on_corpus: bool,
    subject: str = "",
    value: str = "",
    cited_span: str = "",
) -> GroundingResult:
    """Map the spine's two-stage verdicts onto the four-way partition."""
    if supported_on_cited:
        return GroundingResult(label="grounded", decision="proceed",
                               rationale="cited span supports the claim")
    if supported_on_corpus:
        return GroundingResult(
            label="complementary", decision="regenerate",
            rationale="cited span does not support the claim but another ledger span does; re-cite",
        )
    if cited_span and detect_numeric_contradiction(subject, value, cited_span):
        return GroundingResult(
            label="contradicted", decision="replan",
            rationale=f"cited span asserts a different value than '{value}' for this subject",
        )
    return GroundingResult(label="ungrounded", decision="regenerate",
                           rationale="claim is supported nowhere in the corpus")


def worst_decision(decisions: list[str]) -> Decision:
    """Aggregate per-claim decisions into the candidate-level decision."""
    if not decisions:
        return "proceed"
    return max(decisions, key=lambda d: _SEVERITY.get(d, 0))  # type: ignore[return-value]
