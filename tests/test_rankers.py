from __future__ import annotations

from spider_qwen.evidence.models import EvidenceRef
from spider_qwen.modes.contracts import (
    Contact,
    ContactCandidate,
    PricingStatus,
    ProductCandidate,
    QuoteChannel,
    QuoteChannelType,
    ServiceCandidate,
)
from spider_qwen.ranking.contact_ranker import ContactRanker
from spider_qwen.ranking.product_ranker import ProductRanker
from spider_qwen.ranking.service_ranker import ServiceRanker


def _ref() -> EvidenceRef:
    return EvidenceRef(ledger_id="ev_1", url="https://x.sg", snippet_hash="h", retrieved_at="2026-01-01T00:00:00Z")


def test_product_ranker_orders_by_price_quality():
    exact = ProductCandidate(vendor_name="A", website="https://a.sg", pricing_status=PricingStatus.EXACT_PRICE,
                             evidence_completeness=1.0, geo_score=20, evidence_refs=[_ref()])
    quote = ProductCandidate(vendor_name="B", website="https://b.sg", pricing_status=PricingStatus.QUOTE_REQUIRED,
                             evidence_completeness=1.0, geo_score=20, evidence_refs=[_ref()])
    ranked = ProductRanker().rank([quote, exact])
    assert ranked[0].vendor_name == "A"


def test_ranker_excludes_evidence_less_candidates():
    no_ev = ProductCandidate(vendor_name="X", pricing_status=PricingStatus.EXACT_PRICE)
    assert ProductRanker().rank([no_ev]) == []


def test_service_ranker_rewards_email_channel():
    qc = QuoteChannel(type=QuoteChannelType.CONTACT_EMAIL, value="sales@a.sg", evidence_ref=_ref())
    strong = ServiceCandidate(vendor_name="A", website="https://a.sg", service_match_score=1.0,
                              service_match_evidence=True, quote_channel=qc, checklist_completeness=1.0,
                              geo_score=20, evidence_completeness=1.0, evidence_refs=[_ref()])
    weak = ServiceCandidate(vendor_name="B", website="https://b.sg", service_match_score=0.4,
                            service_match_evidence=True, geo_score=6, evidence_completeness=0.5, evidence_refs=[_ref()])
    ranked = ServiceRanker().rank([weak, strong])
    assert ranked[0].vendor_name == "A" and ranked[0].score > ranked[1].score
    assert ranked[0].score_components == {
        "suitability": 55.0,
        "contactability": 22.5,
        "evidence_quality": 20.0,
        "conflict_penalty": 0.0,
        "total": 97.5,
    }


def test_service_quote_channel_is_counted_only_as_contactability():
    ref = _ref()
    email = ServiceCandidate(
        vendor_name="Email", website="https://email.sg", service_match_score=0.6,
        service_match_evidence=True, geo_score=10, checklist_completeness=0.5,
        evidence_completeness=0.8, evidence_refs=[ref],
        quote_channel=QuoteChannel(type=QuoteChannelType.CONTACT_EMAIL,
                                   value="sales@email.sg", evidence_ref=ref),
    )
    page = email.model_copy(update={
        "vendor_name": "Page",
        "quote_channel": QuoteChannel(type=QuoteChannelType.CONTACT_PAGE,
                                       value="https://email.sg/contact", evidence_ref=ref),
    })
    ranker = ServiceRanker()
    email_parts = ranker.components(email)
    page_parts = ranker.components(page)
    assert email_parts["suitability"] == page_parts["suitability"]
    assert email_parts["evidence_quality"] == page_parts["evidence_quality"]
    assert email_parts["contactability"] > page_parts["contactability"]
    assert ranker.score(email) - ranker.score(page) == email_parts["contactability"] - page_parts["contactability"]


def test_service_ranker_prioritises_suitability_over_contact_convenience():
    ref = _ref()
    stronger_fit = ServiceCandidate(
        vendor_name="Stronger fit", website="https://fit.sg", evidence_refs=[ref],
        service_match_score=1.0, service_match_evidence=True, geo_score=20,
        checklist_completeness=1.0, evidence_completeness=0.8,
    )
    convenient_contact = ServiceCandidate(
        vendor_name="Convenient contact", website="https://contact.sg", evidence_refs=[ref],
        service_match_score=0.5, service_match_evidence=True, geo_score=0,
        checklist_completeness=0.0, evidence_completeness=1.0,
        quote_channel=QuoteChannel(type=QuoteChannelType.CONTACT_EMAIL,
                                   value="sales@contact.sg", evidence_ref=ref),
    )
    ranked = ServiceRanker().rank([convenient_contact, stronger_fit])
    assert ranked[0].vendor_name == "Stronger fit"


def test_contact_ranker_scores_business_contact():
    c = ContactCandidate(
        vendor_name="A", website="https://a.sg", geo_score=20, evidence_completeness=1.0,
        evidence_refs=[_ref()],
        contacts=[Contact(type="email", value="sales@a.sg", confidence=0.85, evidence_ref=_ref())],
        validation_signals={"domain_match": True, "cross_source_count": 2},
    )
    ranked = ContactRanker().rank([c])
    assert ranked and ranked[0].score > 0
