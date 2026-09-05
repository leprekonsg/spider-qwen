from __future__ import annotations

from spider_qwen.evidence.models import EvidenceRef
from spider_qwen.extraction.dedupe import dedupe_candidates
from spider_qwen.identity import normalize_supplier_name, registrable_domain
from spider_qwen.modes.contracts import (
    PricingStatus,
    ProductCandidate,
    QuoteChannel,
    QuoteChannelType,
    ServiceCandidate,
)
from spider_qwen.rfq.generator import RFQGenerator


def _ref(number: int) -> EvidenceRef:
    return EvidenceRef(
        ledger_id=f"ev_{number}",
        url=f"https://source{number}.example/evidence",
        snippet_hash=f"h{number}",
        retrieved_at="2026-01-01T00:00:00Z",
    )


def _candidate(
    name: str,
    website: str,
    ref_number: int,
    *,
    service_match: bool = False,
    quote: str | None = None,
    branch: str | None = None,
    marketplace_storefront: str | None = None,
) -> ServiceCandidate:
    ref = _ref(ref_number)
    return ServiceCandidate(
        vendor_name=name,
        website=website,
        country="Singapore",
        branch=branch,
        marketplace_storefront=marketplace_storefront,
        service_match_score=1.0 if service_match else 0.0,
        service_match_evidence=service_match,
        quote_channel=(
            QuoteChannel(
                type=QuoteChannelType.CONTACT_EMAIL,
                value=quote,
                evidence_ref=ref,
            )
            if quote else None
        ),
        evidence_refs=[ref],
    )


def test_psl_aware_domains_do_not_collapse_com_sg_suppliers():
    assert registrable_domain("https://shop.alpha.com.sg/x") == "alpha.com.sg"
    assert registrable_domain("https://beta.com.sg") == "beta.com.sg"
    alpha = _candidate("Alpha", "https://alpha.com.sg", 1)
    beta = _candidate("Beta", "https://beta.com.sg", 2)
    result, merges = dedupe_candidates([alpha, beta])
    assert len(result) == 2
    assert merges == 0


def test_shared_host_and_same_name_on_different_sites_remain_distinct():
    hosted_a = _candidate("Alpha Services", "https://market.example.com/alpha", 1)
    hosted_b = _candidate("Beta Services", "https://market.example.com/beta", 2)
    same_name_elsewhere = _candidate("Alpha Services", "https://alpha-services.sg", 3)
    result, merges = dedupe_candidates([hosted_a, hosted_b, same_name_elsewhere])
    assert len(result) == 3
    assert merges == 0
    assert len({candidate.supplier_id for candidate in result}) == 3


def test_same_name_marketplace_storefronts_and_branches_remain_distinct():
    first = _candidate(
        "Best Supplies", "https://market.example", 1,
        marketplace_storefront="https://market.example/stores/best-sg",
        branch="Singapore",
    )
    second = _candidate(
        "Best Supplies", "https://market.example", 2,
        marketplace_storefront="https://market.example/stores/best-my",
        branch="Malaysia",
    )
    result, merges = dedupe_candidates([first, second])
    assert len(result) == 2
    assert merges == 0


def test_unicode_normalization_preserves_multilingual_names():
    assert normalize_supplier_name("Kärcher Pte. Ltd.") == "kärcher"
    assert normalize_supplier_name("บริษัท รักษาความสะอาด จำกัด")
    assert normalize_supplier_name("ＫＡＩ社 Co.") == "kai社"


def test_consolidation_merges_complementary_fields_and_evidence():
    capability = _candidate(
        "Origin Exterminators Pte Ltd", "https://origin.com.sg/services", 1,
        service_match=True,
    )
    contact = _candidate(
        "Origin Exterminators", "https://www.origin.com.sg/contact", 2,
        quote="sales@origin.com.sg",
    )
    result, merges = dedupe_candidates([capability, contact])
    assert merges == 1
    assert len(result) == 1
    merged = result[0]
    assert merged.service_match_evidence is True
    assert merged.quote_channel is not None
    assert merged.quote_channel.value == "sales@origin.com.sg"
    assert {ref.ledger_id for ref in merged.evidence_refs} == {"ev_1", "ev_2"}
    assert merged.field_claims["quote_channel"][0].evidence_refs == [_ref(2)]
    assert merged.field_claims["quote_channel"][0].evidence_scope == "field"
    assert merged.evidence_completeness == 1.0


def test_consolidation_preserves_alternative_quote_channel_claims():
    first = _candidate(
        "Origin Exterminators", "https://origin.com.sg", 1,
        quote="sales@origin.com.sg",
    )
    second = _candidate(
        "Origin Exterminators Pte Ltd", "https://www.origin.com.sg/contact", 2,
        quote="quotes@origin.com.sg",
    )
    merged = dedupe_candidates([first, second])[0][0]
    assert "quote_channel" not in merged.conflicting_fields
    claims = merged.field_claims["quote_channel"]
    assert len(claims) == 2
    assert {claim.value["value"] for claim in claims} == {
        "sales@origin.com.sg", "quotes@origin.com.sg",
    }
    assert {ref.ledger_id for claim in claims for ref in claim.evidence_refs} == {"ev_1", "ev_2"}


def test_repeated_consolidation_keeps_one_selected_claim_and_prior_conflicts():
    candidates = [
        _candidate(
            "Origin Exterminators", "https://origin.com.sg", index,
            quote=f"quotes{index}@origin.com.sg",
        )
        for index in range(1, 4)
    ]
    merged = dedupe_candidates(candidates)[0][0]
    claims = merged.field_claims["quote_channel"]
    assert len(claims) == 3
    assert sum(claim.is_selected for claim in claims) == 1
    assert merged.conflicting_fields == []


def test_conflicting_product_prices_keep_both_claims_and_coarse_evidence_scope():
    first = ProductCandidate(
        vendor_name="Paper Supply Pte Ltd",
        website="https://paper.com.sg/item",
        country="Singapore",
        product_name="A4 paper",
        product_url="https://paper.com.sg/item",
        price=10.0,
        currency="SGD",
        pricing_status=PricingStatus.EXACT_PRICE,
        evidence_refs=[_ref(1)],
    )
    second = ProductCandidate(
        vendor_name="Paper Supply",
        website="https://www.paper.com.sg/item",
        country="Singapore",
        product_name="A4 paper",
        product_url="https://www.paper.com.sg/item",
        price=12.0,
        currency="SGD",
        pricing_status=PricingStatus.EXACT_PRICE,
        evidence_refs=[_ref(2)],
    )
    merged = dedupe_candidates([first, second])[0][0]
    assert "price" in merged.conflicting_fields
    assert {claim.value for claim in merged.field_claims["price"]} == {10.0, 12.0}
    assert {claim.evidence_scope for claim in merged.field_claims["price"]} == {"candidate"}
    assert {ref.ledger_id for ref in merged.evidence_refs} == {"ev_1", "ev_2"}


def test_supplier_id_is_stable_and_reaches_rfq_vendor():
    candidate = _candidate(
        "Origin Exterminators Pte. Ltd.", "https://www.origin.com.sg/contact", 1,
        service_match=True,
        quote="sales@origin.com.sg",
    )
    equivalent = _candidate(
        "Origin Exterminators", "https://origin.com.sg", 2,
        service_match=True,
    )
    assert candidate.supplier_id == equivalent.supplier_id
    draft = RFQGenerator().generate(query="pest control", candidate=candidate)
    assert draft.vendor.supplier_id == candidate.supplier_id


def test_rfq_uses_selected_channel_while_preserving_alternatives_for_review():
    first = _candidate(
        "Origin Exterminators", "https://origin.com.sg", 1,
        service_match=True, quote="sales@origin.com.sg",
    )
    second = _candidate(
        "Origin Exterminators", "https://origin.com.sg/contact", 2,
        service_match=True, quote="quotes@origin.com.sg",
    )
    candidate = dedupe_candidates([first, second])[0][0]
    draft = RFQGenerator().generate(query="pest control", candidate=candidate)
    assert draft.status == "complete"
    assert draft.quote_channel is not None
    assert draft.rfq_email_template
    assert len(candidate.field_claims["quote_channel"]) == 2
