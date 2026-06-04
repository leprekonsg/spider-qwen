"""MiniCheck-style entailment gatekeeper for (claim, evidence_span) pairs.

The deterministic default upholds a claim only if its concrete value is
groundable in the evidence span -- a normalized substring match, with token
overlap as a partial signal. When ``subject`` is set (vendor-scoped atoms), the value and vendor must
co-occur in the same sentence, or the sentence immediately above a price line,
so a competitor's price on the same or another page cannot verify the claim.

This is the spider-qwen hot-path discipline: a fact counts only when it is
literally present in the cited source, so a fabricated value (whose own
self-referential extraction snippet might "contain" it) is caught when checked
against the real page text.

An optional ``model`` seam supplies a learned NLI score (MiniCheck-FT5). Its
output is type-checked and clamped to [0, 1] and can never raise into the hot
path; the evidence span is untrusted, so the model is treated as a scorer, not an
instruction follower.
"""

from __future__ import annotations

import re
from typing import Callable

from pydantic import BaseModel

# Strip a currency marker only when it directly precedes a digit, so "S$129" ->
# "129". Alpha codes (rm/sgd/usd/...) carry a negative lookbehind so they are only
# stripped as standalone codes, never mid-word ("Transform99" must stay intact).
_CURRENCY = re.compile(
    r"(?:s\$|us\$|\$|£|€|(?<![a-z0-9])(?:rm|sgd|usd|myr|eur|gbp))\s?(?=\d)",
    re.IGNORECASE,
)
_TOKEN = re.compile(r"[a-z0-9@.+]+")
# Pure-numeric values (prices, quantities) must match a whole number in the
# evidence, not a substring -- "129" must not be grounded by "1290" (a postal code
# or year). Decimals compare by value so "129" still matches "129.00".
_NUMERIC = re.compile(r"^\d+(?:\.\d+)?$")
_NUMBER_IN_TEXT = re.compile(r"\d+(?:\.\d+)?")
# Sentence boundary: terminal punctuation FOLLOWED BY whitespace (or newline).
# A bare "." is not a boundary -- emails (sales@acme.sg), domains, and decimal
# prices (129.50) contain dots, and splitting inside them made it impossible
# for such values to ever co-occur with the vendor "in one sentence", silently
# failing every email-valued relation claim.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


# Generic business tokens must not alone "ground" a vendor name in SAFE corpus scans.
_SUBJECT_STOP = frozenset({
    "pte", "ltd", "limited", "co", "company", "corp", "inc", "singapore", "sg",
    "the", "and", "of", "supply", "services", "trading", "enterprise", "group",
    "global", "international", "solutions", "systems",
})


class MiniCheckResult(BaseModel):
    supported: bool
    score: float
    method: str  # value_grounded | relation_grounded | token_overlap | no_evidence | model | subject_ungrounded
    rationale: str = ""


def _norm(text: str) -> str:
    return re.sub(r"[\s,]", "", _CURRENCY.sub("", (text or "").lower()))


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall((text or "").lower()))


def _subject_tokens(subject: str) -> list[str]:
    raw = [t for t in _TOKEN.findall((subject or "").lower()) if t not in _SUBJECT_STOP]
    strong = [t for t in raw if len(t) >= 3]
    if strong:
        return strong
    return [t for t in raw if len(t) >= 2]


def _subject_grounded(subject: str, premise: str) -> bool:
    """True when distinctive vendor tokens appear anywhere in the span."""
    tokens = _subject_tokens(subject)
    if not tokens:
        norm_sub = _norm(subject)
        return bool(norm_sub) and norm_sub in _norm(premise)
    premise_tokens = _tokens(premise)
    hits = sum(1 for t in tokens if t in premise_tokens)
    return hits >= max(1, (len(tokens) + 1) // 2)


def _subject_in_sentence(tokens: list[str], sentence: str) -> bool:
    sent_tokens = _tokens(sentence)
    hits = sum(1 for t in tokens if t in sent_tokens)
    return hits >= max(1, (len(tokens) + 1) // 2)


def _sentences(premise: str) -> list[str]:
    parts = [s.strip() for s in _SENTENCE_SPLIT.split(premise or "") if s and s.strip()]
    return parts if parts else [premise or ""]


def _relation_grounded(subject: str, norm_value: str, premise: str) -> bool:
    """True when the value and vendor co-occur in one sentence or the line above a price."""
    tokens = _subject_tokens(subject)
    sents = _sentences(premise)
    for i, sent in enumerate(sents):
        if not _value_grounded(norm_value, sent):
            continue
        if not tokens:
            norm_sub = _norm(subject)
            if norm_sub and norm_sub in _norm(sent):
                return True
            if i > 0 and norm_sub in _norm(sents[i - 1]):
                return True
        else:
            if _subject_in_sentence(tokens, sent):
                return True
            if i > 0 and _subject_in_sentence(tokens, sents[i - 1]):
                return True
    return False


def _value_grounded(norm_value: str, premise: str) -> bool:
    """Is the (normalized) claim value present in the premise?

    Pure-numeric values match a whole number by value (so "129" matches "129.00"
    but not "1290"); everything else uses a normalized substring match.
    """
    norm_premise = _norm(premise)
    if _NUMERIC.match(norm_value):
        target = float(norm_value)
        return any(abs(float(n) - target) < 1e-9 for n in _NUMBER_IN_TEXT.findall(norm_premise))
    return norm_value in norm_premise


class MiniCheck:
    """Entailment gatekeeper: does the evidence span support the claim's value?"""

    def __init__(self, *, threshold: float = 0.5,
                 model: Callable[[str, str], dict] | None = None) -> None:
        self.threshold = float(threshold)
        self.model = model

    def check(self, *, claim: str, value: str = "", evidence_span: str = "",
              field: str = "", subject: str = "") -> MiniCheckResult:
        premise = evidence_span or ""
        if not premise.strip():
            return MiniCheckResult(supported=False, score=0.0, method="no_evidence",
                                   rationale="no evidence span to ground the claim")
        hypothesis = claim.strip() or value
        result = self._heuristic(claim=hypothesis, value=value, premise=premise, subject=subject)
        if self.model is not None:
            result = self._apply_model(
                result, claim=hypothesis, value=value, premise=premise, subject=subject,
            )
        return result

    def _heuristic(self, *, claim: str, value: str, premise: str,
                   subject: str = "") -> MiniCheckResult:
        relation = bool((subject or "").strip())
        norm_value = _norm(value)
        if norm_value:
            if relation:
                if _relation_grounded(subject, norm_value, premise):
                    return MiniCheckResult(
                        supported=True, score=1.0, method="relation_grounded",
                        rationale=f"value '{value}' grounded with vendor '{subject}' in the same sentence",
                    )
                if _value_grounded(norm_value, premise):
                    return MiniCheckResult(
                        supported=False, score=0.0, method="subject_ungrounded",
                        rationale=(
                            f"value '{value}' appears on the page but not in a sentence "
                            f"that also identifies vendor '{subject}'"
                        ),
                    )
            elif _value_grounded(norm_value, premise):
                return MiniCheckResult(
                    supported=True, score=1.0, method="value_grounded",
                    rationale=f"value '{value}' present in evidence",
                )
            value_tokens = _tokens(value)
            score = round(len(value_tokens & _tokens(premise)) / len(value_tokens), 4) if value_tokens else 0.0
            if relation and score >= self.threshold and not _relation_grounded(subject, norm_value, premise):
                score = 0.0
            return MiniCheckResult(
                supported=score >= self.threshold, score=score, method="token_overlap",
                rationale=f"value '{value}' not grounded; token overlap {score}",
            )
        claim_tokens = _tokens(claim)
        score = round(len(claim_tokens & _tokens(premise)) / len(claim_tokens), 4) if claim_tokens else 0.0
        if relation and score >= self.threshold and not _subject_grounded(subject, premise):
            score = 0.0
        return MiniCheckResult(
            supported=score >= self.threshold, score=score, method="token_overlap",
            rationale=f"claim-text token overlap {score}",
        )

    def _apply_model(
        self,
        base: MiniCheckResult,
        *,
        claim: str,
        value: str,
        premise: str,
        subject: str = "",
    ) -> MiniCheckResult:
        try:
            out = self.model(claim, premise)  # type: ignore[misc]
        except Exception:
            return base
        if not isinstance(out, dict):
            return base
        score = out.get("score")
        if not isinstance(score, (int, float)) or isinstance(score, bool):
            return base
        score = round(max(0.0, min(1.0, float(score))), 4)
        rationale = out.get("rationale")
        supported = score >= self.threshold
        norm_value = _norm(value)
        if supported and (subject or "").strip() and norm_value:
            if not _relation_grounded(subject, norm_value, premise):
                if _value_grounded(norm_value, premise):
                    return MiniCheckResult(
                        supported=False, score=0.0, method="subject_ungrounded",
                        rationale=f"model score {score} rejected: vendor '{subject}' not co-located with value",
                    )
                supported = False
        return MiniCheckResult(
            supported=supported, score=score, method="model",
            rationale=rationale if isinstance(rationale, str) else base.rationale,
        )
