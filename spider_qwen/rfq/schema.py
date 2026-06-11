"""RFQ draft schema (spec section 5.2).

An RFQDraft is never submitted. quote_channel must carry an evidence_ref, and
the draft as a whole carries evidence_refs back into the ledger.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .. import SCHEMA_VERSION
from ..evidence.belief import BeliefInterval
from ..evidence.models import EvidenceRef
from ..modes.contracts import QuoteChannel


class ChecklistItem(BaseModel):
    field: str
    reason: str
    required: bool = True
    evidence_ref: EvidenceRef | None = None


class RFQVendor(BaseModel):
    vendor_name: str
    website: str = ""
    country: str | None = None


class RFQDraft(BaseModel):
    schema_version: str = SCHEMA_VERSION
    status: Literal["complete", "incomplete"] = "incomplete"
    rfq_email_template: str = ""
    required_inputs_checklist: list[ChecklistItem] = Field(default_factory=list)
    quote_channel: QuoteChannel | None = None
    assumptions_and_limits: list[str] = Field(default_factory=list)
    vendor: RFQVendor
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    # Trust surface: the GRADE tier the verification spine assigned to this
    # candidate's claims (None when verification is off) and the DS [Bel, Pl]
    # interval fused over the quote channel's sources (None without a channel).
    evidence_grade: str | None = None
    belief_interval: BeliefInterval | None = None
    # Provenance of the email body: "template" (deterministic) or
    # "qwen:<model>". Qwen-drafted bodies carry a language tag and any numeric
    # claims the deterministic fact-check could not ground in ledger evidence.
    drafted_by: str = "template"
    language: str | None = None
    unsourced_claims: list[str] = Field(default_factory=list)
