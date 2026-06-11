"""Guardrail #3.6 (privacy gate): named-person contacts are tagged
high_sensitivity and gated; generic business (role) contacts pass.

Covers the actual runtime tagging path (ContactExtractor -> _classify_email),
the governance classifier/helper, and the ReviewGate enforcement when enabled.
"""

from __future__ import annotations

from spider_qwen.extraction.contact import ContactExtractor
from spider_qwen.agent.controller import Controller
from spider_qwen.evidence.models import EvidenceRef
from spider_qwen.governance.privacy import classify_field_privacy, is_high_sensitivity
from spider_qwen.governance.review import ReviewGate
from spider_qwen.modes.contracts import Contact, ContactCandidate, PrivacyClass


def _emails(matches):
    return {m.value: m.privacy_class for m in matches if m.type == "email"}


def test_named_person_email_tagged_high_sensitivity():
    text = "Reach jane.doe@acme-corp.com for enquiries, or sales@acme-corp.com."
    emails = _emails(ContactExtractor().extract(text))
    assert emails["jane.doe@acme-corp.com"] == PrivacyClass.NAMED_PERSON_HIGH_SENSITIVITY


def test_role_email_is_business_contact():
    emails = _emails(ContactExtractor().extract("Email sales@acme-corp.com or info@acme-corp.com."))
    assert emails["sales@acme-corp.com"] == PrivacyClass.BUSINESS_CONTACT
    assert emails["info@acme-corp.com"] == PrivacyClass.BUSINESS_CONTACT


def test_classify_field_privacy_and_helper():
    assert classify_field_privacy("named_person_email") == PrivacyClass.NAMED_PERSON_HIGH_SENSITIVITY
    assert classify_field_privacy("company_email") == PrivacyClass.BUSINESS_CONTACT
    assert is_high_sensitivity(PrivacyClass.NAMED_PERSON_HIGH_SENSITIVITY) is True
    assert is_high_sensitivity(PrivacyClass.BUSINESS_CONTACT) is False


class _Policy:
    """Minimal policy with the gate enabled for named persons only."""

    def review_gate_enabled(self, privacy_class: str) -> bool:
        return privacy_class == PrivacyClass.NAMED_PERSON_HIGH_SENSITIVITY.value


def test_review_gate_gates_named_person_passes_business():
    gate = ReviewGate(_Policy())
    assert gate.requires_review(PrivacyClass.NAMED_PERSON_HIGH_SENSITIVITY) is True
    assert gate.requires_review(PrivacyClass.BUSINESS_CONTACT) is False


def test_review_gate_disabled_by_default():
    """With no review_gate config (v1 default), nothing is gated."""

    class _Off:
        def review_gate_enabled(self, _pc: str) -> bool:
            return False

    gate = ReviewGate(_Off())
    assert gate.requires_review(PrivacyClass.NAMED_PERSON_HIGH_SENSITIVITY) is False


def test_run_output_redacts_high_sensitivity_contacts_by_policy():
    ref = EvidenceRef(
        ledger_id="ev_1",
        url="https://acme.example",
        snippet_hash="h",
        retrieved_at="2026-01-01T00:00:00+00:00",
    )
    candidate = ContactCandidate(
        vendor_name="Acme",
        evidence_refs=[ref],
        contacts=[
            Contact(
                type="email",
                value="jane.doe@acme.example",
                confidence=0.7,
                privacy_class=PrivacyClass.NAMED_PERSON_HIGH_SENSITIVITY,
                evidence_ref=ref,
            ),
            Contact(
                type="email",
                value="sales@acme.example",
                confidence=0.9,
                privacy_class=PrivacyClass.BUSINESS_CONTACT,
                evidence_ref=ref,
            ),
        ],
    )
    public = Controller(offline=True, state_dir=None, persist=False)._public_candidate_dump(candidate)
    values = [c["value"] for c in public["contacts"]]
    assert "[redacted:high_sensitivity_contact]" in values
    assert "sales@acme.example" in values
    assert public["validation_signals"]["redacted_contacts"] == 1
