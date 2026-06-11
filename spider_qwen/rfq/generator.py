"""RFQ draft generator with the v1 hard-stop rules.

Hard stops:
- No evidenced quote channel  -> no polished RFQ; status incomplete.
- Checklist completeness below the policy threshold -> status incomplete.

The output never submits or sends anything. Tone is SEA-market-neutral
professional English: short, direct, polite.
"""

from __future__ import annotations

from ..evidence.belief import BeliefInterval
from ..evidence.models import EvidenceRef
from ..modes.contracts import QuoteChannelType, ServiceCandidate
from .checklist import RFQChecklistBuilder, compute_checklist_completeness
from .schema import RFQDraft, RFQVendor

_DRAFT_DISCLAIMER = "This is a draft only. spider-qwen does not send or submit RFQs in v1."


class RFQGenerator:
    def __init__(self, tone: str = "SEA-neutral professional English; short and direct",
                 minimum_completeness: float = 0.65, drafter: object | None = None) -> None:
        self.tone = tone
        self.minimum_completeness = minimum_completeness
        self.drafter = drafter  # optional Qwen body drafter; template on None/failure
        self._checklist = RFQChecklistBuilder()

    def generate(
        self,
        *,
        query: str,
        candidate: ServiceCandidate,
        target_country: str | None = None,
        evidence_grade: str | None = None,
        belief_interval: BeliefInterval | None = None,
        evidence_corpus: str | None = None,
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

        # Trust surface: how confident the buyer should be in this draft's
        # grounding, stated rather than implied.
        if evidence_grade is not None:
            assumptions.append(f"Evidence grade (GRADE): {evidence_grade}.")
        if belief_interval is not None:
            assumptions.append(
                f"Quote channel belief interval [Bel, Pl] = "
                f"[{belief_interval.belief}, {belief_interval.plausibility}]"
                f" (uncertainty {belief_interval.uncertainty})."
            )

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
                evidence_grade=evidence_grade,
                belief_interval=belief_interval,
            )

        # Hard stop 2: insufficient grounding -> incomplete.
        status = "complete" if completeness >= self.minimum_completeness else "incomplete"
        if status == "incomplete":
            assumptions.append(
                f"Checklist completeness {completeness:.2f} is below the {self.minimum_completeness:.2f} threshold."
            )

        email, drafted_by, language, unsourced = self._draft_body(
            query, candidate, evidence_corpus, assumptions
        )
        return RFQDraft(
            status=status,
            rfq_email_template=email,
            required_inputs_checklist=checklist,
            quote_channel=candidate.quote_channel,
            assumptions_and_limits=assumptions,
            vendor=vendor,
            evidence_refs=refs,
            evidence_grade=evidence_grade,
            belief_interval=belief_interval,
            drafted_by=drafted_by,
            language=language,
            unsourced_claims=unsourced,
        )

    def _draft_body(
        self,
        query: str,
        candidate: ServiceCandidate,
        evidence_corpus: str | None,
        assumptions: list[str],
    ) -> tuple[str, str, str | None, list[str]]:
        """Qwen-drafted body with deterministic fact-check, else the template.

        CoVe split: the model only writes prose; ``unsourced_numeric_claims``
        independently flags every quantitative claim the ledger evidence (or
        the buyer's own query) cannot ground. Flags are stated on the draft,
        never silently dropped, and any drafter failure falls back to the
        deterministic template.
        """
        if self.drafter is not None:
            from .factcheck import unsourced_numeric_claims

            channel = candidate.quote_channel
            try:
                drafted = self.drafter.draft(
                    query=query,
                    vendor_name=candidate.vendor_name,
                    country=candidate.country,
                    quote_channel=channel.type.value if channel else None,
                )
            except Exception:
                drafted = None
            if drafted is not None and drafted.body.strip():
                flags = unsourced_numeric_claims(
                    drafted.body, f"{evidence_corpus or ''} {query}"
                )
                drafted_by = f"qwen:{getattr(self.drafter, 'model', '')}"
                assumptions.append(
                    f"RFQ body drafted by Qwen ({drafted_by}); deterministic "
                    f"fact-check flagged {len(flags)} unsourced numeric claim(s)."
                )
                for flag in flags:
                    assumptions.append(f"Unsourced claim flagged for review: '{flag}'.")
                return drafted.body, drafted_by, drafted.language, flags
            assumptions.append("Qwen drafter unavailable; deterministic template used.")
        return self._render_email(query, candidate), "template", None, []

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
