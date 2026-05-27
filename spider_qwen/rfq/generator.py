"""RFQ draft generator with the v1 hard-stop rules.

Hard stops:
- No evidenced quote channel  -> no polished RFQ; status incomplete.
- Checklist completeness below the policy threshold -> status incomplete.

The output never submits or sends anything. Tone is SEA-market-neutral
professional English: short, direct, polite.
"""

from __future__ import annotations

from ..evidence.models import EvidenceRef
from ..modes.contracts import QuoteChannelType, ServiceCandidate
from .checklist import RFQChecklistBuilder, compute_checklist_completeness
from .schema import RFQDraft, RFQVendor

_DRAFT_DISCLAIMER = "This is a draft only. spider-qwen does not send or submit RFQs in v1."


class RFQGenerator:
    def __init__(self, tone: str = "SEA-neutral professional English; short and direct",
                 minimum_completeness: float = 0.65) -> None:
        self.tone = tone
        self.minimum_completeness = minimum_completeness
        self._checklist = RFQChecklistBuilder()

    def generate(
        self,
        *,
        query: str,
        candidate: ServiceCandidate,
        target_country: str | None = None,
    ) -> RFQDraft:
        vendor = RFQVendor(
            vendor_name=candidate.vendor_name,
            website=candidate.website or "",
            country=candidate.country,
        )
        checklist = self._checklist.build(query=query, candidate=candidate, target_country=target_country)
        completeness = compute_checklist_completeness(candidate, target_country)
        candidate.checklist_completeness = completeness

        assumptions = [
            _DRAFT_DISCLAIMER,
            "Vendor service scope inferred from public web content; confirm with vendor.",
            f"Requested service derived from query: '{query}'.",
        ]
        refs: list[EvidenceRef] = list(candidate.evidence_refs)

        # Hard stop 1: no evidenced quote channel -> do not write a polished RFQ.
        if candidate.quote_channel is None:
            assumptions.append("No quote channel was evidenced; polished RFQ withheld.")
            return RFQDraft(
                status="incomplete",
                rfq_email_template="",
                required_inputs_checklist=checklist,
                quote_channel=None,
                assumptions_and_limits=assumptions,
                vendor=vendor,
                evidence_refs=refs,
            )

        # Hard stop 2: insufficient grounding -> incomplete.
        status = "complete" if completeness >= self.minimum_completeness else "incomplete"
        if status == "incomplete":
            assumptions.append(
                f"Checklist completeness {completeness:.2f} is below the {self.minimum_completeness:.2f} threshold."
            )

        email = self._render_email(query, candidate)
        return RFQDraft(
            status=status,
            rfq_email_template=email,
            required_inputs_checklist=checklist,
            quote_channel=candidate.quote_channel,
            assumptions_and_limits=assumptions,
            vendor=vendor,
            evidence_refs=refs,
        )

    def _render_email(self, query: str, candidate: ServiceCandidate) -> str:
        channel = candidate.quote_channel
        addressed = f"Dear {candidate.vendor_name} Team,"
        via = ""
        if channel and channel.type == QuoteChannelType.CONTACT_EMAIL:
            via = f" (sent to {channel.value})"
        return (
            f"{addressed}\n\n"
            f"We are sourcing quotations for: {query}.\n\n"
            "Could you please provide a quotation covering scope of work, pricing basis, "
            "service frequency/schedule, and contract terms? We have outlined the inputs "
            "we can share in the attached checklist and would appreciate guidance on any "
            "additional details you require to quote.\n\n"
            "Kindly include your standard lead time and any minimum contract conditions.\n\n"
            f"Thank you,\nProcurement Team{via}"
        )
