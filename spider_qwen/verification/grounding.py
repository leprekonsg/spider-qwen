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

import re
from typing import Literal

from pydantic import BaseModel

from . import minicheck as _mc

GroundingLabel = Literal["grounded", "complementary", "contradicted", "ungrounded"]
Decision = Literal["proceed", "regenerate", "replan"]

# Single source of truth for label -> decision routing.
_DECISION: dict[str, Decision] = {
    "grounded": "proceed",
    "complementary": "regenerate",
    "ungrounded": "regenerate",
    "contradicted": "replan",
}
# Severity order for aggregating a per-candidate decision.
_SEVERITY = {"proceed": 0, "regenerate": 1, "replan": 2}

# A number only counts as a *competing value* when it sits in price/quantity
# context. Street addresses, years, phone fragments, and digits inside the
# vendor's own name ("3M") must never fabricate a contradiction.
_COMPETING_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_VALUE_CONTEXT = re.compile(
    r"US\$|S\$|[$€£¥]"
    r"|\b(?:usd|sgd|myr|eur|gbp|rm|jpy|cny|idr|thb|php|vnd"
    r"|price[sd]?|pricing|cost[s]?|charge[sd]?|fee[s]?|rate[s]?"
    r"|quote[sd]?|quotation[s]?|moq|per|unit[s]?|pc[s]?|piece[s]?)\b",
    re.IGNORECASE,
)
_CONTEXT_WINDOW = 24  # chars around the number searched for value context


def _competing_numbers(sentence: str) -> list[float]:
    """Numbers in the sentence that plausibly assert a price/quantity value."""
    out: list[float] = []
    for m in _COMPETING_NUMBER.finditer(sentence):
        window = sentence[max(0, m.start() - _CONTEXT_WINDOW): m.end() + _CONTEXT_WINDOW]
        if not _VALUE_CONTEXT.search(window):
            continue
        try:
            out.append(float(m.group().replace(",", "")))
        except ValueError:
            continue
    return out


def _scrub_subject(sentence: str, tokens: list[str]) -> str:
    """Remove the subject's own tokens so digits in a vendor name ("3M")
    cannot read as numeric values asserted about the vendor."""
    for tok in tokens:
        sentence = re.sub(rf"\b{re.escape(tok)}\b", " ", sentence, flags=re.IGNORECASE)
    return sentence


class GroundingResult(BaseModel):
    label: GroundingLabel
    decision: Decision
    rationale: str = ""


def detect_numeric_contradiction(subject: str, value: str, premise: str) -> bool:
    """Does the cited span assert a *different* number for this subject?

    Only pure-numeric values (price, MOQ) can auto-contradict, and only when
    the sentence carries a competing number in price/quantity context (a
    currency mark or a price/quote/MOQ/unit keyword nearby). Addresses,
    years, phone numbers, and digits inside the subject's own name never
    contradict. Non-numeric values never auto-contradict (conservative:
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
        scrubbed = _scrub_subject(sent, tokens)
        all_numbers = [float(n) for n in _mc._NUMBER_IN_TEXT.findall(_mc._norm(scrubbed))]
        if any(abs(n - target) < 1e-9 for n in all_numbers):
            return False  # the claimed value is co-located after all
        if _competing_numbers(scrubbed):
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
        label, rationale = "grounded", "cited span supports the claim"
    elif supported_on_corpus:
        label = "complementary"
        rationale = "cited span does not support the claim but another ledger span does; re-cite"
    elif cited_span and detect_numeric_contradiction(subject, value, cited_span):
        label = "contradicted"
        rationale = f"cited span asserts a different value than '{value}' for this subject"
    else:
        label, rationale = "ungrounded", "claim is supported nowhere in the corpus"
    return GroundingResult(label=label, decision=_DECISION[label], rationale=rationale)


def worst_decision(decisions: list[str]) -> Decision:
    """Aggregate per-claim decisions into the candidate-level decision.

    Fail closed: an unrecognized decision string maps to the most severe
    outcome (replan) rather than silently scoring as proceed.
    """
    if not decisions:
        return "proceed"
    known = [d if d in _SEVERITY else "replan" for d in decisions]
    return max(known, key=_SEVERITY.__getitem__)  # type: ignore[return-value]
