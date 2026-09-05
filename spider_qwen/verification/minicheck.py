"""MiniCheck-style entailment gatekeeper for (claim, evidence_span) pairs.

The deterministic default upholds a claim only if its concrete value is
groundable in the evidence span -- a normalized substring match, with token
overlap as a partial signal. When ``subject`` is set (vendor-scoped atoms), the
value and vendor must co-occur in the same sentence, or the sentence immediately
above a price line. This co-location heuristic reduces cross-vendor collisions;
it is not full entity resolution.

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
# Hard semantic guards operate on the clause that contains the concrete value.
# Commas inside numbers are preserved; ordinary commas, semicolons, and explicit
# contrast words delimit clauses so a negation about another fact cannot poison
# a valid claim elsewhere in the sentence.
_CLAUSE_SPLIT = re.compile(
    r"\s*(?:;|(?<!\d),(?!\d)|\b(?:and|but|however)\b)\s*", re.IGNORECASE,
)

HARD_REJECTION_METHODS = frozenset({
    "negated", "historical_scope", "currency_mismatch", "unit_mismatch",
    "qualifier_mismatch", "predicate_mismatch",
})

_NEGATION = re.compile(
    r"\b(?:not|never|no longer|cannot|can't|do not|don't|does not|doesn't|"
    r"is not|isn't|are not|aren't|was not|wasn't|were not|weren't)\b",
    re.IGNORECASE,
)
_HISTORICAL = re.compile(
    r"\b(?:previously|formerly|used to|no longer|expired|discontinued|"
    r"historical|old (?:price|rate|cost)|prior (?:price|rate|cost)|last year)\b"
    r"|\b(?:price|rate|cost)\s+(?:was|were)\s+(?!updated\b|revised\b|changed\b|set\b)",
    re.IGNORECASE,
)
_CURRENT = re.compile(
    r"\b(?:now|currently|current|today|latest|still|effective)\b",
    re.IGNORECASE,
)
_NON_EXACT_PRICE = re.compile(
    r"\b(?:from|starting (?:from|at)|starts? at|as low as|up to)\b",
    re.IGNORECASE,
)

_RAW_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_CURRENCY_TOKEN = r"US\$|S\$|SGD|USD|RM|MYR|EUR|GBP|IDR|Rp|THB|VND|PHP|[$€£฿₫₱]"
_CURRENCY_MAP = {
    "s$": "SGD", "sgd": "SGD", "us$": "USD", "usd": "USD",
    "rm": "MYR", "myr": "MYR", "eur": "EUR", "€": "EUR",
    "gbp": "GBP", "£": "GBP", "idr": "IDR", "rp": "IDR",
    "thb": "THB", "฿": "THB", "vnd": "VND", "₫": "VND",
    "php": "PHP", "₱": "PHP",
}
_UNIT_AFTER_VALUE = re.compile(
    rf"^\s*(?:{_CURRENCY_TOKEN})?\s*(?:(?:per|/)\s*)?"
    r"(units?|items?|pieces?|pcs?|each|months?|monthly|years?|annual(?:ly)?|"
    r"hours?|hrs?|hourly|sq\s*ft|sqft|square feet|sq\s*m|sqm|square metres?|"
    r"pax|persons?|days?|seats?|users?|kg|kilograms?|tons?|tonnes?)\b",
    re.IGNORECASE,
)
_UNIT_ALIASES = {
    "unit": "unit", "units": "unit", "item": "unit", "items": "unit",
    "piece": "unit", "pieces": "unit", "pc": "unit", "pcs": "unit", "each": "unit",
    "month": "month", "months": "month", "monthly": "month",
    "year": "year", "years": "year", "annual": "year", "annually": "year",
    "hour": "hour", "hours": "hour", "hr": "hour", "hrs": "hour", "hourly": "hour",
    "sqft": "sqft", "sq ft": "sqft", "square feet": "sqft",
    "sqm": "sqm", "sq m": "sqm", "square metre": "sqm", "square metres": "sqm",
    "pax": "person", "person": "person", "persons": "person",
    "day": "day", "days": "day", "seat": "seat", "seats": "seat",
    "user": "user", "users": "user", "kg": "kg", "kilogram": "kg", "kilograms": "kg",
    "ton": "ton", "tons": "ton", "tonne": "ton", "tonnes": "ton",
}
_QUOTE_POSITIVE = re.compile(
    r"\b(?:quotes?|quotations?|rfqs?|pricing|estimates?|proposals?|tenders?|enquir(?:y|ies))\b",
    re.IGNORECASE,
)
_QUOTE_EXCLUDED = re.compile(
    r"\b(?:only\s+for\s+)?(?:purchase\s+)?(?:orders?|technical support|customer support|"
    r"returns?|billing|careers?|job applications?)\s+only\b"
    r"|\bonly\s+for\s+(?:purchase\s+)?(?:orders?|technical support|customer support|"
    r"returns?|billing|careers?|job applications?)\b",
    re.IGNORECASE,
)


# Generic business tokens must not alone "ground" a vendor name in SAFE corpus scans.
_SUBJECT_STOP = frozenset({
    "pte", "ltd", "limited", "co", "company", "corp", "inc", "singapore", "sg",
    "the", "and", "of", "supply", "services", "trading", "enterprise", "group",
    "global", "international", "solutions", "systems",
})


class MiniCheckResult(BaseModel):
    supported: bool
    score: float
    method: str  # value_grounded | relation_grounded | token_overlap | no_evidence | no_value | model | subject_ungrounded
    rationale: str = ""
    # Set by SAFE re-verification only: the corpus span that produced this
    # verdict, so a complementary claim's citation can be re-pointed to it.
    winning_span: str = ""


def _norm(text: str) -> str:
    """Lowercase, strip currency marks, drop thousands separators, collapse
    whitespace. Token boundaries are PRESERVED: "S$1,299 50 pcs" must become
    "1299 50 pcs", never "129950pcs" -- merging adjacent digit runs makes the
    real price (1299) unfindable while grounding a fabricated one (129950)."""
    t = _CURRENCY.sub("", (text or "").lower())
    t = re.sub(r"(?<=\d),(?=\d)", "", t)  # 1,299 -> 1299 (digit-group commas only)
    t = t.replace(",", " ")
    return re.sub(r"\s+", " ", t).strip()


def _tokens(text: str) -> set[str]:
    return set(_TOKEN.findall((text or "").lower()))


def _subject_tokens(subject: str) -> list[str]:
    raw = [t for t in _TOKEN.findall((subject or "").lower()) if t not in _SUBJECT_STOP]
    strong = [t for t in raw if len(t) >= 3]
    if strong:
        return strong
    return [t for t in raw if len(t) >= 2]


def _subject_in_sentence(tokens: list[str], sentence: str) -> bool:
    sent_tokens = _tokens(sentence)
    hits = sum(1 for t in tokens if t in sent_tokens)
    return hits >= max(1, (len(tokens) + 1) // 2)


def _sentences(premise: str) -> list[str]:
    parts = [s.strip() for s in _SENTENCE_SPLIT.split(premise or "") if s and s.strip()]
    return parts if parts else [premise or ""]


def _clauses(sentence: str) -> list[str]:
    parts = [c.strip() for c in _CLAUSE_SPLIT.split(sentence or "") if c and c.strip()]
    return parts if parts else [sentence or ""]


def _relation_value_clauses(subject: str, norm_value: str, premise: str) -> list[str]:
    """Value-bearing clauses whose sentence is scoped to ``subject``."""
    tokens = _subject_tokens(subject)
    norm_sub = _norm(subject)
    sents = _sentences(premise)
    out: list[str] = []
    for i, sent in enumerate(sents):
        if not _value_grounded(norm_value, sent):
            continue
        if tokens:
            scoped = _subject_in_sentence(tokens, sent) or (
                i > 0 and _subject_in_sentence(tokens, sents[i - 1])
            )
        else:
            scoped = bool(norm_sub) and (
                norm_sub in _norm(sent) or (i > 0 and norm_sub in _norm(sents[i - 1]))
            )
        if scoped:
            out.extend(c for c in _clauses(sent) if _value_grounded(norm_value, c))
    return out


def _relation_grounded(subject: str, norm_value: str, premise: str) -> bool:
    """True when the value and vendor co-occur in one sentence or the line above a price."""
    return bool(_relation_value_clauses(subject, norm_value, premise))


def value_grounded(value: str, premise: str) -> bool:
    """Is the value present in the premise, under MiniCheck normalization?

    Public seam so callers pre-filtering on groundedness (e.g. preferring a
    span-grounded quote channel) use the SAME notion the verification spine
    applies later -- a raw substring check would diverge on currency symbols
    and whitespace.
    """
    norm_value = _norm(value)
    return bool(norm_value) and _value_grounded(norm_value, premise or "")


def relation_grounded(subject: str, value: str, premise: str) -> bool:
    """Public seam: the spine's relation gate (value + vendor in one sentence).

    Callers pre-filtering vendor-scoped extractions (e.g. quote channels) must
    use the SAME co-location notion the verification spine applies later; a
    value-only pre-filter can prefer a match the spine then rejects.
    """
    norm_value = _norm(value)
    return bool(norm_value) and _relation_grounded(subject, norm_value, premise or "")


def _value_grounded(norm_value: str, premise: str) -> bool:
    """Is the (normalized) claim value present in the premise?

    Pure-numeric values match a whole number by value (so "129" matches "129.00"
    but not "1290"); everything else matches on token boundaries, so a short
    value ("3M") cannot ground inside an unrelated run ("3 mm", "acme3m").
    """
    norm_premise = _norm(premise)
    if _NUMERIC.match(norm_value):
        target = float(norm_value)
        return any(abs(float(n) - target) < 1e-9 for n in _NUMBER_IN_TEXT.findall(norm_premise))
    return bool(re.search(
        rf"(?<![a-z0-9]){re.escape(norm_value)}(?![a-z0-9])", norm_premise
    ))


def _matching_number_spans(value: str, clause: str) -> list[re.Match[str]]:
    norm_value = _norm(value)
    if not _NUMERIC.match(norm_value):
        return []
    target = float(norm_value)
    matches: list[re.Match[str]] = []
    for match in _RAW_NUMBER.finditer(clause):
        try:
            number = float(match.group(0).replace(",", ""))
        except ValueError:
            continue
        if abs(number - target) < 1e-9:
            matches.append(match)
    return matches


def _currency_for_number(match: re.Match[str], clause: str) -> str:
    before = clause[max(0, match.start() - 12):match.start()]
    after = clause[match.end():match.end() + 12]
    token_match = re.search(rf"({_CURRENCY_TOKEN})\s*$", before, re.IGNORECASE)
    if token_match is None:
        token_match = re.match(rf"^\s*({_CURRENCY_TOKEN})(?![a-z])", after, re.IGNORECASE)
    if token_match is None:
        return ""
    # Bare "$" depends on page locale/URL, which MiniCheck does not have.
    # Treat it as unspecified rather than inventing a currency mismatch.
    return _CURRENCY_MAP.get(token_match.group(1).lower(), "")


def _unit_for_number(match: re.Match[str], clause: str) -> str:
    unit_match = _UNIT_AFTER_VALUE.match(clause[match.end():match.end() + 48])
    if not unit_match:
        return ""
    raw = re.sub(r"\s+", " ", unit_match.group(1).lower()).strip()
    return _UNIT_ALIASES.get(raw, "")


def _normalized_unit(unit: str) -> str:
    raw = re.sub(r"\s+", " ", (unit or "").strip().lower())
    raw = re.sub(r"^(?:per|/)\s*", "", raw)
    return _UNIT_ALIASES.get(raw, raw)


def _normalized_currency(currency: str) -> str:
    raw = (currency or "").strip().lower()
    return _CURRENCY_MAP.get(raw, raw.upper())


def _is_non_exact_price(value: str, clause: str) -> bool:
    if _NON_EXACT_PRICE.search(clause):
        return True
    for match in _matching_number_spans(value, clause):
        before = clause[max(0, match.start() - 32):match.start()]
        after = clause[match.end():match.end() + 32]
        if re.match(
            rf"^\s*(?:-|–|to)\s*(?:{_CURRENCY_TOKEN})?\s*{_RAW_NUMBER.pattern}",
            after, re.IGNORECASE,
        ):
            return True
        if re.search(
            rf"{_RAW_NUMBER.pattern}\s*(?:-|–|to)\s*(?:{_CURRENCY_TOKEN})?\s*$",
            before, re.IGNORECASE,
        ):
            return True
    return False


def _price_qualifier_failure(
    *, value: str, clause: str, currency: str, unit: str,
) -> tuple[str, str] | None:
    """Check currency and unit on the same numeric occurrence."""
    occurrences = [
        (_currency_for_number(match, clause), _unit_for_number(match, clause))
        for match in _matching_number_spans(value, clause)
    ]
    if not occurrences:
        return None
    expected_currency = _normalized_currency(currency) if currency else ""
    expected_unit = _normalized_unit(unit) if unit else ""
    has_currency_scope = any(observed_currency for observed_currency, _ in occurrences)
    has_unit_scope = any(observed_unit for _, observed_unit in occurrences)

    def compatible(observed_currency: str, observed_unit: str) -> bool:
        currency_ok = (
            not expected_currency or not has_currency_scope
            or observed_currency == expected_currency
        )
        unit_ok = not expected_unit or not has_unit_scope or observed_unit == expected_unit
        return currency_ok and unit_ok

    if any(compatible(*occurrence) for occurrence in occurrences):
        return None

    observed_currencies = {c for c, _ in occurrences if c}
    observed_units = {u for _, u in occurrences if u}
    if expected_currency and all(c and c != expected_currency for c, _ in occurrences):
        return (
            "currency_mismatch",
            f"the value is explicitly denominated in {', '.join(sorted(observed_currencies))}, "
            f"not {expected_currency}",
        )
    if expected_unit and all(u and u != expected_unit for _, u in occurrences):
        return (
            "unit_mismatch",
            f"the value is explicitly scoped per {', '.join(sorted(observed_units))}, "
            f"not per {expected_unit}",
        )
    return (
        "qualifier_mismatch",
        f"no occurrence of value '{value}' has both currency {expected_currency or 'unspecified'} "
        f"and unit {expected_unit or 'unspecified'}",
    )


def _hard_rejection(
    *, field: str, subject: str, value: str, premise: str,
    currency: str = "", unit: str = "", pricing_status: str = "",
    channel_type: str = "",
) -> MiniCheckResult | None:
    """Return an explicit contradiction/scope verdict, if every grounding is disqualified."""
    norm_value = _norm(value)
    semantic_field = (
        field in {"price", "moq", "quote_channel"} or field.startswith("contact_")
    )
    if not norm_value or not semantic_field:
        return None
    clauses = (_relation_value_clauses(subject, norm_value, premise)
               if subject.strip()
               else [c for s in _sentences(premise) for c in _clauses(s)
                     if _value_grounded(norm_value, c)])
    if not clauses:
        return None

    failures: list[tuple[str, str]] = []
    for clause in clauses:
        # "not only" adds information; it does not negate the value-bearing fact.
        negation_scope = re.sub(r"\bnot\s+only\b", "", clause, flags=re.IGNORECASE)
        if _NEGATION.search(negation_scope):
            failures.append(("negated", "the value-bearing clause explicitly negates the claimed fact"))
            continue
        if _HISTORICAL.search(clause) and not _CURRENT.search(clause):
            failures.append(("historical_scope", "the value is stated only in historical or inactive context"))
            continue
        if (field == "price" and pricing_status.upper() == "EXACT_PRICE"
                and _is_non_exact_price(value, clause)):
            failures.append((
                "predicate_mismatch",
                "the evidence explicitly qualifies the value as non-exact",
            ))
            continue
        if field == "price" and (currency or unit):
            qualifier_failure = _price_qualifier_failure(
                value=value, clause=clause, currency=currency, unit=unit,
            )
            if qualifier_failure:
                failures.append(qualifier_failure)
                continue
        if field == "quote_channel" and channel_type:
            if _QUOTE_EXCLUDED.search(clause) and not _QUOTE_POSITIVE.search(clause):
                failures.append((
                    "predicate_mismatch",
                    "the endpoint is explicitly limited to a non-quotation purpose",
                ))
                continue
        # One clean, subject-scoped mention is sufficient. A stale duplicate
        # elsewhere on the page cannot invalidate an explicit current fact.
        return None

    if failures:
        method, rationale = failures[0]
        return MiniCheckResult(supported=False, score=0.0, method=method, rationale=rationale)
    return None


class MiniCheck:
    """Entailment gatekeeper: does the evidence span support the claim's value?"""

    def __init__(self, *, threshold: float = 0.5,
                 model: Callable[[str, str], dict] | None = None) -> None:
        self.threshold = float(threshold)
        self.model = model

    def check(self, *, claim: str, value: str = "", evidence_span: str = "",
              field: str = "", subject: str = "", currency: str = "",
              unit: str = "", pricing_status: str = "",
              channel_type: str = "") -> MiniCheckResult:
        premise = evidence_span or ""
        if not premise.strip():
            return MiniCheckResult(supported=False, score=0.0, method="no_evidence",
                                   rationale="no evidence span to ground the claim")
        hypothesis = claim.strip() or value
        hard_rejection = _hard_rejection(
            field=field, subject=subject, value=value, premise=premise,
            currency=currency, unit=unit, pricing_status=pricing_status,
            channel_type=channel_type,
        )
        if hard_rejection is not None:
            # Learned NLI is a scorer for ambiguity. It may not override an
            # explicit contradiction or schema-level scope mismatch.
            return hard_rejection
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
            # Generic business tokens (pte/ltd/trading/...) must not carry the
            # overlap on their own: a wrong vendor name would verify against any
            # page of legal boilerplate. Distinctive-token coverage is required
            # IN ADDITION to overall coverage (min of the two ratios), so the
            # rule only ever fails more closed than plain overlap. A value with
            # ONLY generic tokens needs every token present.
            value_tokens = _tokens(value)
            premise_tokens = _tokens(premise)
            strong = value_tokens - _SUBJECT_STOP
            required = self.threshold if strong else 1.0
            if not value_tokens:
                score = 0.0
            else:
                score = len(value_tokens & premise_tokens) / len(value_tokens)
                if strong:
                    score = min(score, len(strong & premise_tokens) / len(strong))
                score = round(score, 4)
            if relation and score >= required and not _relation_grounded(subject, norm_value, premise):
                score = 0.0
            return MiniCheckResult(
                supported=score >= required, score=score, method="token_overlap",
                rationale=f"value '{value}' not grounded; token overlap {score}",
            )
        if relation:
            # A relation claim with no concrete value (e.g. an empty-valued quote
            # channel proposed upstream) has nothing to ground; template words
            # ("accepts quote requests via") must not verify it. Fail closed.
            return MiniCheckResult(
                supported=False, score=0.0, method="no_value",
                rationale="relation claim carries no concrete value to ground",
            )
        claim_tokens = _tokens(claim)
        score = round(len(claim_tokens & _tokens(premise)) / len(claim_tokens), 4) if claim_tokens else 0.0
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
        norm_value_guard = _norm(value)
        if (subject or "").strip() and not norm_value_guard:
            return base  # nothing concrete to ground; the no_value verdict stands
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
