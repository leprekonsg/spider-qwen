"""Explicit buyer requirements and conservative source-assertion assessments.

Phrase support is deliberately narrower than semantic qualification. Unparsed
queries, paraphrases, third-party assertions and ambiguous scope remain unresolved.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .evidence.models import EvidenceRef
from .identity import normalize_supplier_name


class Requirement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    requirement_id: str = Field(default="", max_length=100)
    text: str = Field(min_length=2, max_length=1000)
    kind: Literal["mandatory", "preferred", "exclusion"] = "mandatory"
    scope: Literal["supplier", "offering"] = "supplier"

    @model_validator(mode="after")
    def normalize(self):
        self.text = self.text.strip()
        if len(self.text) < 2:
            raise ValueError("Requirement text must describe a supplier condition.")
        if not self.requirement_id:
            seed = f"{self.kind}|{self.scope}|{_norm(self.text)}"
            self.requirement_id = "req_" + hashlib.sha256(seed.encode()).hexdigest()[:16]
        return self


class ProcurementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=10_000)
    requirements: list[Requirement] = Field(default_factory=list, max_length=100)
    requirements_confirmed: bool = False
    # Buyer/operator-confirmed ownership bindings. Merely extracting a company
    # name from a page on this host is not proof that the supplier owns the site.
    supplier_sources: dict[str, list[str]] = Field(default_factory=dict, max_length=100)

    @model_validator(mode="after")
    def check_requirements(self):
        ids = [r.requirement_id for r in self.requirements]
        if len(ids) != len(set(ids)):
            raise ValueError("Requirement IDs must be unique; combine duplicate conditions.")
        if self.requirements_confirmed and not self.requirements:
            raise ValueError("Provide at least one requirement before confirming the checklist.")
        for supplier, origins in self.supplier_sources.items():
            if not supplier.strip() or not origins or len(origins) > 20:
                raise ValueError("Each supplier source binding needs a supplier name and 1–20 website origins.")
            for origin in origins:
                parsed = urlsplit(origin)
                if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
                    raise ValueError("Supplier source bindings must be website origins, for example https://acme.sg.")
        return self


class RequirementAssessment(BaseModel):
    requirement_id: str
    text: str
    kind: Literal["mandatory", "preferred", "exclusion"]
    scope: Literal["supplier", "offering"]
    supplier_id: str
    offering_id: str = ""
    status: Literal["supported", "contradicted", "not_found"] = "not_found"
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    observed_at: str | None = None
    reason: str = "no_current_scoped_assertion"
    # Exact excerpts remain auditable without pretending that observation time
    # establishes when the publisher's assertion became valid.
    excerpts: list[str] = Field(default_factory=list)


def _norm(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _contains(text: str, phrase: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(_norm(phrase)) + r"(?!\w)", _norm(text)))


_NEGATIVE = re.compile(r"\b(?:not(?! only\b)|never|no longer|cannot|can't|don't|doesn't|isn't|aren't|won't)\b", re.I)
_UNCERTAIN = re.compile(r"\b(?:previously|formerly|expired|historical|used to|might|may|if|subject to|planned)\b", re.I)


def assess_requirements(request: ProcurementRequest, candidate, ledger) -> list[RequirementAssessment]:
    """Assess explicit phrases on current, candidate-linked first-party pages.

    A repeated affirmative and negative assertion is retained as unresolved.
    Nothing is inferred from absent text, a search snippet, or a memory recall.
    """
    host = (urlsplit(candidate.website or "").hostname or "").casefold().removeprefix("www.")
    approved_hosts = {
        (urlsplit(origin).hostname or "").casefold().removeprefix("www.")
        for name, origins in request.supplier_sources.items()
        if normalize_supplier_name(name) == normalize_supplier_name(candidate.vendor_name)
        for origin in origins
    }
    assessments = []
    for requirement in request.requirements:
        assessment = RequirementAssessment(
            **requirement.model_dump(), supplier_id=candidate.supplier_id,
            offering_id=getattr(candidate, "offering_id", ""),
        )
        signs = set()
        if host not in approved_hosts:
            assessment.reason = "supplier_source_ownership_unconfirmed"
            assessments.append(assessment)
            continue
        for ref in candidate.evidence_refs:
            item = ledger.get(ref.ledger_id)
            if item is None or not item.text or item.source_tool in {"semantic_memory", "mcp_search", "tinyfish_search", "wayback_cdx"}:
                continue
            source_host = (urlsplit(item.final_url or item.url).hostname or "").casefold().removeprefix("www.")
            if not host or source_host != host:
                continue
            supplier_name = normalize_supplier_name(candidate.vendor_name)
            for sentence in re.split(r"(?<=[.!?])\s+|\n+|;", item.text):
                sentence = sentence.replace("’", "'")
                if not _contains(sentence, requirement.text):
                    continue
                prefix = _norm(sentence).split(_norm(requirement.text), 1)[0]
                subject = re.escape(supplier_name) if supplier_name else r"(?!)"
                assertion = re.search(
                    rf"(?<!\w)(?:{subject}|we)\s+(?:currently\s+)?"
                    r"(?:(?:do|does|can|will)\s+not\s+|cannot\s+|can't\s+|don't\s+|doesn't\s+|won't\s+|can\s+)?"
                    r"(?:offers?|provides?|supports?|serves?|covers?|uses?|(?:are|is)\s+(?:not\s+)?certified)"
                    r"(?:\s+(?:both|in|for))?\s*$", prefix,
                )
                if assertion is None:
                    continue
                if requirement.scope == "offering":
                    scope = getattr(candidate, "offer_scope", None)
                    name = getattr(scope, "item", "") or getattr(candidate, "product_name", "")
                    if not name or not _contains(sentence, name):
                        continue
                # Require an assertion, not navigation text or a buyer question.
                if "?" in sentence or _UNCERTAIN.search(sentence):
                    continue
                signs.add("contradicted" if _NEGATIVE.search(assertion.group()) else "supported")
                if ref.ledger_id not in {r.ledger_id for r in assessment.evidence_refs}:
                    assessment.evidence_refs.append(ref)
                assessment.excerpts.append(sentence[:2000])
        if len(signs) == 1:
            assessment.status = signs.pop()
            assessment.reason = "explicit_publisher_assertion"
        elif len(signs) > 1:
            assessment.reason = "conflicting_assertions"
        if assessment.evidence_refs:
            assessment.observed_at = max(r.retrieved_at for r in assessment.evidence_refs)
        assessments.append(assessment)
    return assessments


def qualification(request: ProcurementRequest, assessments: list[RequirementAssessment]) -> dict:
    required = [a for a in assessments if a.kind != "preferred"]
    def satisfied(a):
        return a.status == ("contradicted" if a.kind == "exclusion" else "supported")
    failed = [a.requirement_id for a in required if a.status != "not_found" and not satisfied(a)]
    missing = [a.requirement_id for a in required if a.status == "not_found"]
    complete = request.requirements_confirmed and bool(required)
    return {
        "status": "not_qualified" if failed else "unresolved" if missing or not complete else "qualified",
        "requirements_confirmed": request.requirements_confirmed,
        "mandatory_count": len(required),
        "mandatory_supported": sum(satisfied(a) for a in required),
        "unresolved_requirement_ids": missing,
        "failed_requirement_ids": failed,
        "unparsed_request": None if request.requirements_confirmed else request.query,
        "basis": "publisher_assertions_not_independent_truth",
    }
