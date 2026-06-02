"""MiniCheck-style entailment gatekeeper for (claim, evidence_span) pairs.

The deterministic default upholds a claim only if its concrete value is
groundable in the evidence span -- a normalized substring match, with token
overlap as a partial signal. This is the spider-qwen hot-path discipline: a fact
counts only when it is literally present in the cited source, so a fabricated
value (whose own self-referential extraction snippet might "contain" it) is
caught when checked against the real page text.

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


class MiniCheckResult(BaseModel):
    supported: bool
    score: float
    method: str  # value_grounded | token_overlap | no_evidence | model
    rationale: str = ""


def _norm(text: str) -> str:
    return re.sub(r"[\s,]", "", _CURRENCY.sub("", (text or "").lower()))


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall((text or "").lower()))


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
              field: str = "") -> MiniCheckResult:
        premise = evidence_span or ""
        if not premise.strip():
            return MiniCheckResult(supported=False, score=0.0, method="no_evidence",
                                   rationale="no evidence span to ground the claim")
        result = self._heuristic(claim=claim, value=value, premise=premise)
        if self.model is not None:
            result = self._apply_model(result, claim=claim, premise=premise)
        return result

    def _heuristic(self, *, claim: str, value: str, premise: str) -> MiniCheckResult:
        norm_value = _norm(value)
        if norm_value:
            if _value_grounded(norm_value, premise):
                return MiniCheckResult(supported=True, score=1.0, method="value_grounded",
                                       rationale=f"value '{value}' present in evidence")
            value_tokens = _tokens(value)
            score = round(len(value_tokens & _tokens(premise)) / len(value_tokens), 4) if value_tokens else 0.0
            return MiniCheckResult(supported=score >= self.threshold, score=score,
                                   method="token_overlap",
                                   rationale=f"value '{value}' not grounded; token overlap {score}")
        claim_tokens = _tokens(claim)
        score = round(len(claim_tokens & _tokens(premise)) / len(claim_tokens), 4) if claim_tokens else 0.0
        return MiniCheckResult(supported=score >= self.threshold, score=score, method="token_overlap",
                               rationale=f"claim-text token overlap {score}")

    def _apply_model(self, base: MiniCheckResult, *, claim: str, premise: str) -> MiniCheckResult:
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
        return MiniCheckResult(supported=score >= self.threshold, score=score, method="model",
                               rationale=rationale if isinstance(rationale, str) else base.rationale)
