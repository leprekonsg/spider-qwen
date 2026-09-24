"""T-2.2 verification spine: atomic decomposition -> MiniCheck -> SAFE.

Acceptance: an injected unsupported claim is caught (verified=false) and blocked
from output. The deterministic gatekeeper grounds a claim's value against its
source *page* text (not the self-referential extraction snippet), so a fabricated
value present nowhere in the real evidence fails MiniCheck and SAFE alike.
"""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

from spider_qwen.evidence.ledger import EvidenceLedger
from spider_qwen.evidence.verifier import VerificationSpine
from spider_qwen.modes.contracts import PricingStatus, QuoteChannelType
from spider_qwen.verification.atomic import decompose
from spider_qwen.verification.minicheck import MiniCheck
from spider_qwen.verification.safe import SafeReverifier
from spider_qwen.tools.fetch_service import MockFetchProvider
from spider_qwen.tools.provider_types import SearchResult, SearchResultSet


# --- atomic decomposition --------------------------------------------------

def test_decompose_product_yields_price_and_vendor_claims():
    cand = SimpleNamespace(
        vendor_name="Acme Pte Ltd", price=129.0, currency="SGD", unit="unit",
        moq="50", pricing_status=PricingStatus.EXACT_PRICE, evidence_refs=[],
    )
    claims = {c.field: c for c in decompose(cand)}
    assert set(claims) == {"vendor_name", "price", "moq"}
    assert claims["price"].object_value == "129"  # 129.0 -> "129"
    assert claims["price"].critical is True  # priced status -> critical
    assert claims["vendor_name"].critical is False
    assert claims["moq"].critical is False


def test_decompose_skips_unknown_vendor_and_missing_price():
    cand = SimpleNamespace(vendor_name="Unknown Vendor", price=None,
                           pricing_status=PricingStatus.NOT_FOUND, evidence_refs=[])
    assert decompose(cand) == []


# --- MiniCheck gatekeeper --------------------------------------------------

def test_minicheck_value_present_is_grounded():
    r = MiniCheck().check(claim="price is 129", value="129",
                          evidence_span="Public pricing S$129 per unit. MOQ 50.")
    assert r.supported and r.score == 1.0 and r.method == "value_grounded"


def test_minicheck_empty_span_is_unsupported():
    r = MiniCheck().check(claim="price is 999", value="999", evidence_span="")
    assert not r.supported and r.score == 0.0 and r.method == "no_evidence"


def test_minicheck_value_absent_is_unsupported():
    r = MiniCheck().check(claim="price is 999", value="999",
                          evidence_span="We supply chairs. Pricing S$129 per unit.")
    assert not r.supported


def test_minicheck_normalizes_phone_and_email():
    span = "Contact sales@acme.sg or call +65 6123 4567 for a quote."
    assert MiniCheck().check(claim="email", value="sales@acme.sg", evidence_span=span).supported
    assert MiniCheck().check(claim="phone", value="+65 6123 4567", evidence_span=span).supported


def test_minicheck_model_seam_is_clamped_and_typechecked():
    def junk_model(claim, premise):
        return {"score": 99.0}  # out of range -> clamped to 1.0

    r = MiniCheck(model=junk_model).check(claim="x", value="zzz",
                                          evidence_span="text with zzz in it")
    assert r.method == "model" and 0.0 <= r.score <= 1.0


def test_minicheck_model_cannot_vouch_for_value_absent_from_evidence():
    def optimistic(claim, premise):
        return {"score": 0.99}

    r = MiniCheck(model=optimistic).check(claim="x", value="zzz", evidence_span="unrelated text")
    assert r.supported is False
    assert r.method == "value_ungrounded"


def test_minicheck_model_nan_score_falls_back_to_heuristic():
    def nan_model(claim, premise):
        return {"score": float("nan")}

    r = MiniCheck(model=nan_model).check(claim="x", value="zzz", evidence_span="unrelated text")
    assert r.supported is False
    assert r.method != "model"


def test_minicheck_numeric_value_requires_whole_number_match():
    mc = MiniCheck()
    # A fabricated price must not be grounded by a coincidental substring of a
    # longer number (postal code / year / phone).
    assert not mc.check(claim="price 129", value="129",
                        evidence_span="Office at postal code 1290. Open daily.").supported
    # ...but a genuine standalone price grounds, including trailing-zero decimals.
    assert mc.check(claim="price 129", value="129", evidence_span="Price S$129 each.").supported
    assert mc.check(claim="price 129", value="129", evidence_span="Price S$129.00 each.").supported


def test_norm_does_not_strip_currency_codes_inside_words():
    from spider_qwen.verification.minicheck import _norm
    assert _norm("Transform99") == "transform99"  # "rm" mid-word preserved
    assert _norm("Platform101") == "platform101"
    assert _norm("RM50") == "50"  # standalone currency code still stripped
    assert _norm("S$129") == "129"


# --- SAFE re-verification --------------------------------------------------

def test_safe_grounds_claim_against_corpus():
    mc = MiniCheck()
    claim = decompose(SimpleNamespace(vendor_name="Acme", price=129.0,
                                      pricing_status=PricingStatus.EXACT_PRICE,
                                      evidence_refs=[]))[1]  # the price claim
    res = SafeReverifier(mc).reverify(claim, corpus=["Elsewhere: Acme S$129 per unit."])
    assert res.supported and res.method.startswith("safe_")


def test_safe_unsupported_when_no_source_grounds_it():
    mc = MiniCheck()
    claim = decompose(SimpleNamespace(vendor_name="Acme", price=999.0,
                                      pricing_status=PricingStatus.EXACT_PRICE,
                                      evidence_refs=[]))[1]
    res = SafeReverifier(mc).reverify(claim, corpus=["Acme S$129 per unit.", "Other vendor S$200."])
    assert not res.supported


def test_minicheck_rejects_same_page_cross_vendor_price_collision():
    mc = MiniCheck()
    page = (
        "Shadow Supply offers ergonomic chairs in Singapore. "
        "Public pricing S$129 per unit. "
        "Acme Chairs also lists S$999 per unit for similar stock."
    )
    assert mc.check(
        claim="Shadow lists 129", value="129", evidence_span=page, subject="Shadow Supply",
    ).supported
    assert not mc.check(
        claim="Shadow lists 999", value="999", evidence_span=page, subject="Shadow Supply",
    ).supported


def test_spine_blocks_same_page_cross_vendor_price_collision():
    ledger = EvidenceLedger("run_test", None)
    page_text = (
        "Shadow Supply offers chairs in Singapore. Public pricing S$129 per unit. "
        "Acme Chairs also lists S$999 per unit."
    )
    page_ref = ledger.record(
        source_tool="tinyfish_fetch", url="https://shadow-supply.sg",
        snippet=page_text[:120], text=page_text, metadata={},
    )
    claim_ref = ledger.record(
        source_tool="tinyfish_fetch", url="https://shadow-supply.sg",
        snippet="S$999 per unit", text=None,
        metadata={"field": "pricing", "claim_id": "claim_shadow_999",
                  "parent_ledger_id": page_ref.ledger_id},
    )
    cand = SimpleNamespace(
        vendor_name="Shadow Supply", price=999.0, currency="SGD", unit="unit",
        moq=None, pricing_status=PricingStatus.EXACT_PRICE,
        evidence_refs=[page_ref, claim_ref],
    )
    cv = VerificationSpine(ledger).verify_candidate(cand)
    assert cv.verified is False
    price = next(c for c in cv.claims if c.field == "price")
    assert price.verified is False


def test_safe_rejects_cross_vendor_value_collision():
    mc = MiniCheck()
    claim = decompose(SimpleNamespace(vendor_name="Shadow Supply", price=999.0,
                                      pricing_status=PricingStatus.EXACT_PRICE,
                                      evidence_refs=[]))[1]
    res = SafeReverifier(mc).reverify(
        claim, corpus=["Acme Chairs supplies chairs. Public pricing S$999 per unit."],
    )
    assert not res.supported


def test_minicheck_requires_subject_in_span_when_provided():
    mc = MiniCheck()
    span = "Acme Chairs supplies chairs. Public pricing S$999 per unit."
    assert mc.check(claim="price 999", value="999", evidence_span=span).supported
    assert not mc.check(claim="Shadow lists price 999", value="999", evidence_span=span,
                        subject="Shadow Supply").supported


def test_safe_search_fn_seam_supplies_grounding():
    mc = MiniCheck()
    claim = decompose(SimpleNamespace(vendor_name="Acme", price=129.0,
                                      pricing_status=PricingStatus.EXACT_PRICE,
                                      evidence_refs=[]))[1]
    res = SafeReverifier(mc, search_fn=lambda q: ["fresh result: Acme S$129 per unit"]).reverify(
        claim, corpus=[])
    assert res.supported


def test_safe_search_fn_failure_is_logged(caplog):
    mc = MiniCheck()
    claim = decompose(SimpleNamespace(vendor_name="Acme", price=129.0,
                                      pricing_status=PricingStatus.EXACT_PRICE,
                                      evidence_refs=[]))[1]

    def boom(_query):
        raise RuntimeError("search unavailable")

    with caplog.at_level("WARNING", logger="spider_qwen.verification.safe"):
        res = SafeReverifier(mc, search_fn=boom).reverify(claim, corpus=[])

    assert not res.supported
    assert "search unavailable" in caplog.text


# --- deterministic semantic scope guards ----------------------------------

def _verify_price_text(page_text, *, price=129.0, currency="SGD", unit="unit", model=None):
    ledger = EvidenceLedger("run_scope", None)
    page_ref, claim_ref = _record_page_and_claim(
        ledger, page_text=page_text, claim_value=str(int(price)), grounded=False,
    )
    candidate = SimpleNamespace(
        vendor_name="Acme Pte Ltd", price=price, currency=currency, unit=unit,
        moq=None, pricing_status=PricingStatus.EXACT_PRICE,
        evidence_refs=[page_ref, claim_ref],
    )
    spine = VerificationSpine(ledger, minicheck=MiniCheck(model=model))
    result = spine.verify_candidate(candidate)
    return result, next(c for c in result.claims if c.field == "price")


def test_spine_rejects_negated_price_but_not_unrelated_negation():
    rejected, price = _verify_price_text(
        "Acme Pte Ltd does not list SGD 129 per unit; current price is SGD 149 per unit."
    )
    assert rejected.verified is False
    assert price.rationale == "the value-bearing clause explicitly negates the claimed fact"

    accepted, price = _verify_price_text(
        "Acme Pte Ltd does not charge a setup fee and current price is SGD 129 per unit."
    )
    assert accepted.verified is True
    assert price.verified is True

    additive, price = _verify_price_text(
        "Acme Pte Ltd not only lists SGD 129 per unit but also includes delivery."
    )
    assert additive.verified is True
    assert price.verified is True


def test_spine_rejects_historical_price_and_accepts_current_price():
    rejected, price = _verify_price_text(
        "Acme Pte Ltd old price was SGD 129 per unit. Current price is SGD 149 per unit."
    )
    assert rejected.verified is False
    assert "historical or inactive" in price.rationale

    accepted, price = _verify_price_text(
        "Acme Pte Ltd previously listed SGD 99 per unit. Current price is SGD 129 per unit."
    )
    assert accepted.verified is True
    assert price.verified is True

    repeated, price = _verify_price_text(
        "Acme Pte Ltd previously listed SGD 129 per unit. Current price is SGD 129 per unit."
    )
    assert repeated.verified is True
    assert price.verified is True


def test_spine_rejects_explicit_currency_and_unit_mismatches():
    wrong_currency, currency_claim = _verify_price_text(
        "Acme Pte Ltd current price is USD 129 per unit."
    )
    assert wrong_currency.verified is False
    assert "denominated in USD, not SGD" in currency_claim.rationale

    wrong_us_symbol, currency_claim = _verify_price_text(
        "Acme Pte Ltd current price is US$129 per unit."
    )
    assert wrong_us_symbol.verified is False
    assert "denominated in USD, not SGD" in currency_claim.rationale

    wrong_unit, unit_claim = _verify_price_text(
        "Acme Pte Ltd current price is SGD 129 per month."
    )
    assert wrong_unit.verified is False
    assert "per month, not per unit" in unit_claim.rationale

    accepted, price = _verify_price_text(
        "Acme Pte Ltd current price is SGD 129 per piece."
    )
    assert accepted.verified is True
    assert price.verified is True  # piece and unit are equivalent product units

    unspecified, price = _verify_price_text("Acme Pte Ltd current price is 129.")
    assert unspecified.verified is True
    assert price.verified is True  # absent scope is unknown, not an explicit mismatch


def test_spine_does_not_combine_qualifiers_across_price_occurrences():
    rejected, price = _verify_price_text(
        "Acme Pte Ltd offers USD 129 per month or SGD 129 per year.",
        unit="month",
    )
    assert rejected.verified is False
    assert price.rationale == (
        "no occurrence of value '129' has both currency SGD and unit month"
    )

    accepted, price = _verify_price_text(
        "Acme Pte Ltd offers USD 129 per year or SGD 129 per month.",
        unit="month",
    )
    assert accepted.verified is True
    assert price.verified is True


def test_spine_rejects_non_exact_evidence_for_exact_price_claim():
    rejected, price = _verify_price_text(
        "Acme Pte Ltd prices start from SGD 129 per unit."
    )
    assert rejected.verified is False
    assert price.rationale == "the evidence explicitly qualifies the value as non-exact"

    ranged, price = _verify_price_text(
        "Acme Pte Ltd lists a range of SGD 129-199 per unit."
    )
    assert ranged.verified is False
    assert price.rationale == "the evidence explicitly qualifies the value as non-exact"


def _verify_quote_text(page_text):
    ledger = EvidenceLedger("run_quote_scope", None)
    page_ref = ledger.record(
        source_tool="tinyfish_fetch", url="https://acme.sg/contact",
        snippet=page_text, text=page_text, metadata={},
    )
    quote_ref = ledger.record(
        source_tool="tinyfish_fetch", url="https://acme.sg/contact",
        snippet="sales@acme.sg", text=None,
        metadata={"field": "quote_channel", "claim_id": "quote_scope",
                  "parent_ledger_id": page_ref.ledger_id},
    )
    channel = SimpleNamespace(
        type=QuoteChannelType.CONTACT_EMAIL,
        value="sales@acme.sg", evidence_ref=quote_ref,
    )
    candidate = SimpleNamespace(
        vendor_name="Acme Pte Ltd", quote_channel=channel,
        evidence_refs=[page_ref, quote_ref],
    )
    result = VerificationSpine(ledger).verify_candidate(candidate)
    return result, next(c for c in result.claims if c.field == "quote_channel")


def test_spine_requires_endpoint_not_be_explicitly_limited_to_another_purpose():
    rejected, quote = _verify_quote_text(
        "Acme Pte Ltd uses sales@acme.sg for purchase orders only."
    )
    assert rejected.verified is False
    assert "non-quotation purpose" in quote.rationale

    accepted, quote = _verify_quote_text(
        "Acme Pte Ltd uses sales@acme.sg for sales and quotations."
    )
    assert accepted.verified is True
    assert quote.verified is True


def test_spine_accepts_generic_sales_email_as_quote_channel_when_not_excluded():
    accepted, quote = _verify_quote_text(
        "Acme Pte Ltd can be reached at sales@acme.sg."
    )
    assert accepted.verified is True
    assert quote.verified is True


def test_model_and_safe_cannot_override_hard_scope_rejection():
    optimistic_model = lambda claim, premise: {"score": 1.0, "rationale": "entails"}
    result, price = _verify_price_text(
        "Acme Pte Ltd old price was SGD 129 per unit.", model=optimistic_model,
    )
    assert result.verified is False
    assert price.verifier_score == 0.0
    assert "historical or inactive" in price.rationale

    claim = decompose(SimpleNamespace(
        vendor_name="Acme Pte Ltd", price=129.0, currency="SGD", unit="unit",
        pricing_status=PricingStatus.EXACT_PRICE, evidence_refs=[],
    ))[1]
    safe = SafeReverifier(MiniCheck(model=optimistic_model)).reverify(
        claim, corpus=["Acme Pte Ltd old price was SGD 129 per unit."],
    )
    assert safe.supported is False
    assert safe.method == "safe_historical_scope"
    assert "historical or inactive" in safe.rationale


# --- spine over a ledger ---------------------------------------------------

def _record_page_and_claim(ledger, *, page_text, claim_value, grounded):
    """Record a page row + a pricing claim row, with valid offsets only if grounded."""
    page_ref = ledger.record(source_tool="tinyfish_fetch", url="https://acme.sg/chairs",
                             snippet=page_text[:200], text=page_text, metadata={})
    metadata = {"extraction": "pricing", "field": "pricing", "matched_text": claim_value,
                "claim_id": "claim_test1", "parent_ledger_id": page_ref.ledger_id}
    if grounded:
        start = page_text.find(claim_value)
        metadata.update({"start_char": start, "end_char": start + len(claim_value)})
    claim_ref = ledger.record(source_tool="tinyfish_fetch", url="https://acme.sg/chairs",
                              snippet=claim_value, text=None, metadata=metadata)
    return page_ref, claim_ref


def test_spine_verifies_grounded_candidate_and_writes_back():
    ledger = EvidenceLedger("run_test", None)
    page_ref, claim_ref = _record_page_and_claim(
        ledger, page_text="Acme supplies chairs in Singapore. Public pricing S$129 per unit.",
        claim_value="S$129", grounded=True,
    )
    cand = SimpleNamespace(vendor_name="Acme", price=129.0, currency="SGD", unit="unit",
                           moq=None, pricing_status=PricingStatus.EXACT_PRICE,
                           evidence_refs=[page_ref, claim_ref])
    cv = VerificationSpine(ledger).verify_candidate(cand)

    assert cv.verified is True
    price = next(c for c in cv.claims if c.field == "price")
    assert price.verified is True and price.verifier_score == 1.0
    # verified/verifier_score written onto the claim ledger row
    item = ledger.get(claim_ref.ledger_id)
    assert item.metadata.get("verified") is True
    assert item.metadata.get("verifier_score") == 1.0


def test_spine_blocks_cross_vendor_safe_collision():
    ledger = EvidenceLedger("run_test", None)
    acme_page = ledger.record(
        source_tool="tinyfish_fetch", url="https://acme-chairs.sg",
        snippet="Acme pricing", text="Acme Chairs. Public pricing S$999 per unit.",
        metadata={},
    )
    shadow_page = ledger.record(
        source_tool="tinyfish_fetch", url="https://shadow-supply.sg",
        snippet="Shadow pricing", text="Shadow Supply offers chairs. Public pricing S$129 per unit.",
        metadata={},
    )
    claim_ref = ledger.record(
        source_tool="tinyfish_fetch", url="https://shadow-supply.sg",
        snippet="S$999 per unit", text=None,
        metadata={"field": "pricing", "claim_id": "claim_shadow_price",
                  "parent_ledger_id": shadow_page.ledger_id},
    )
    cand = SimpleNamespace(
        vendor_name="Shadow Supply", price=999.0, currency="SGD", unit="unit",
        moq=None, pricing_status=PricingStatus.EXACT_PRICE,
        evidence_refs=[shadow_page, acme_page, claim_ref],
    )
    cv = VerificationSpine(ledger).verify_candidate(cand)
    assert cv.verified is False
    price = next(c for c in cv.claims if c.field == "price")
    assert price.verified is False and "safe" in price.stage


def test_spine_blocks_injected_unsupported_claim():
    # Fabricated price: the matched_text is its own snippet (offsets absent), but
    # the real page text never mentions 999 -> MiniCheck and SAFE both fail.
    ledger = EvidenceLedger("run_test", None)
    page_ref, claim_ref = _record_page_and_claim(
        ledger, page_text="Acme supplies chairs in Singapore. Public pricing S$129 per unit.",
        claim_value="S$999 per unit", grounded=False,
    )
    cand = SimpleNamespace(vendor_name="Acme", price=999.0, currency="SGD", unit="unit",
                           moq=None, pricing_status=PricingStatus.EXACT_PRICE,
                           evidence_refs=[page_ref, claim_ref])
    cv = VerificationSpine(ledger).verify_candidate(cand)

    assert cv.verified is False  # critical price claim unsupported -> candidate blocked
    price = next(c for c in cv.claims if c.field == "price")
    assert price.verified is False and "safe" in price.stage
    assert ledger.get(claim_ref.ledger_id).metadata.get("verified") is False


def test_spine_treats_priced_value_with_unknown_status_as_critical():
    # A populated price with a missing/unrecognized status must still be critical,
    # so a fabricated price cannot slip through on a blank status field.
    ledger = EvidenceLedger("run_test", None)
    page_ref, claim_ref = _record_page_and_claim(
        ledger, page_text="Acme supplies chairs in Singapore. Public pricing S$129 per unit.",
        claim_value="S$999 per unit", grounded=False,
    )
    cand = SimpleNamespace(vendor_name="Acme", price=999.0, currency="SGD", unit="unit",
                           moq=None, pricing_status=None, evidence_refs=[page_ref, claim_ref])
    cv = VerificationSpine(ledger).verify_candidate(cand)
    price = next(c for c in cv.claims if c.field == "price")
    assert price.critical is True
    assert cv.verified is False


def test_spine_blocks_candidate_with_no_claims():
    ledger = EvidenceLedger("run_test", None)
    cv = VerificationSpine(ledger).verify_candidate(SimpleNamespace(evidence_refs=[]))
    assert cv.verified is False
    assert cv.verifier_score == 0.0
    assert cv.unsupported_critical == ["no_claims"]
    assert cv.claims == []


def test_spine_accepts_candidate_with_failed_noncritical_claim():
    # A failed non-critical claim (vendor name not in body) must not block a
    # candidate whose critical price claim is grounded.
    ledger = EvidenceLedger("run_test", None)
    page_ref, claim_ref = _record_page_and_claim(
        ledger, page_text="GhostVendor supplies chairs. Pricing S$129 per unit. Ergonomic chairs in stock.",
        claim_value="S$129", grounded=True,
    )
    # Legal name on the candidate is stricter than the short name on the page.
    cand = SimpleNamespace(vendor_name="GhostVendor Pte Ltd", price=129.0, currency="SGD",
                           unit="unit", moq=None, pricing_status=PricingStatus.EXACT_PRICE,
                           evidence_refs=[page_ref, claim_ref])
    cv = VerificationSpine(ledger).verify_candidate(cand)
    vendor = next(c for c in cv.claims if c.field == "vendor_name")
    price = next(c for c in cv.claims if c.field == "price")
    assert vendor.verified is False and vendor.critical is False
    assert price.verified is True
    assert cv.verified is True  # non-critical failure does not block


def test_spine_does_not_self_verify_semantic_memory_claim():
    # A memory-recalled fact (snippet == value, no parent page) must not ground
    # itself; with no corpus support its critical claim is blocked.
    ledger = EvidenceLedger("run_test", None)
    mem_ref = ledger.record(source_tool="semantic_memory", url="semantic-memory",
                            snippet="quotes@ghost.example", text=None,
                            metadata={"field": "quote_channel", "claim_id": "mem1"})
    qc = SimpleNamespace(value="quotes@ghost.example", evidence_ref=mem_ref)
    cand = SimpleNamespace(vendor_name="Ghost", quote_channel=qc, evidence_refs=[mem_ref])
    cv = VerificationSpine(ledger).verify_candidate(cand)
    qclaim = next(c for c in cv.claims if c.field == "quote_channel")
    assert qclaim.verified is False
    assert cv.verified is False


def test_spine_blocks_injected_quote_channel():
    # A fabricated quote channel absent from the cited page is blocked.
    ledger = EvidenceLedger("run_test", None)
    page_ref = ledger.record(source_tool="tinyfish_fetch", url="https://acme.sg",
                             snippet="Acme cleaning", text="Acme cleaning. Email real@acme.sg.",
                             metadata={})
    qc_ref = ledger.record(source_tool="tinyfish_fetch", url="https://acme.sg",
                           snippet="ghost@evil.example", text=None,
                           metadata={"field": "quote_channel", "claim_id": "q1",
                                     "parent_ledger_id": page_ref.ledger_id})
    qc = SimpleNamespace(value="ghost@evil.example", evidence_ref=qc_ref)
    cand = SimpleNamespace(vendor_name="Acme", quote_channel=qc, evidence_refs=[page_ref, qc_ref])
    cv = VerificationSpine(ledger).verify_candidate(cand)
    qclaim = next(c for c in cv.claims if c.field == "quote_channel")
    assert qclaim.verified is False
    assert cv.verified is False


# --- policy flag -----------------------------------------------------------

def test_verification_disabled_by_default():
    from spider_qwen.agent.policy import load_policy
    assert load_policy().verification_enabled() is False


def test_verification_enabled_via_env(monkeypatch):
    from spider_qwen.agent.policy import load_policy
    monkeypatch.setenv("SPIDER_QWEN_VERIFICATION_ENABLED", "1")
    assert load_policy().verification_enabled() is True


# --- controller acceptance -------------------------------------------------

class _FixedSearch:
    provider_name = "mock"
    search_source_tool = "mock"

    def __init__(self, urls):
        self._urls = urls

    async def search(self, query, location, language, limit):
        results = [SearchResult(url=u, title=u, snippet="office chairs supplier pricing",
                                rank=i, source_tool="mock")
                   for i, u in enumerate(self._urls)]
        return SearchResultSet(query=query, location=location, results=results,
                               total_results=len(results), provider="mock")


class _InjectingQwen:
    """Qwen extractor stub that fabricates an ungrounded price for one URL."""

    def __init__(self, fake_url):
        self.fake_url = fake_url

    def extract(self, *, text, page_url, query):
        from spider_qwen.tools.qwen_json_extractor import QwenPageExtraction, QwenPricingExtraction
        if self.fake_url in page_url:
            return QwenPageExtraction(pricing=QwenPricingExtraction(
                status=PricingStatus.EXACT_PRICE, price=999.0, currency="S$",
                unit="unit", matched_text="S$999 per unit"))
        return QwenPageExtraction()  # grounded path -> deterministic extraction used


# A sentence on Shadow's page that prices another vendor's stock. A Qwen claim
# quoting it passes the extraction gate (located on the page, states the price)
# and only the spine's vendor co-location check can reject it.
_CROSS_VENDOR_SENTENCE = " Acme Chairs also lists S$999 per unit."


def _product_controller(verify, *, shadow_extra=""):
    from spider_qwen.agent.controller import Controller
    good = "https://acme-chairs.sg/ergonomic"
    bad = "https://shadow-supply.sg/ergonomic"
    fixtures = {
        good: {"title": "Acme Chairs", "text": "Acme supplies ergonomic office chairs in "
               "Singapore. Public pricing S$129 per unit. MOQ 50 units. Email sales@acme-chairs.sg."},
        bad: {"title": "Shadow Supply", "text": "Shadow Supply offers ergonomic office chairs "
              "in Singapore. Public pricing S$129 per unit. MOQ 50 units. Email sales@shadow-supply.sg."
              + shadow_extra},
    }
    controller = Controller(
        search_provider=_FixedSearch([good, bad]),
        fetch_provider=MockFetchProvider(fixtures=fixtures),
        qwen_json_extractor=_InjectingQwen("shadow-supply"),
        verify=verify, state_dir=None, persist=False,
    )
    return controller, good, bad


def test_controller_blocks_injected_unsupported_claim_from_output():
    controller, good, bad = _product_controller(verify=True, shadow_extra=_CROSS_VENDOR_SENTENCE)
    result = asyncio.run(controller.run("ergonomic office chairs Singapore",
                                        mode="product_exact_price"))
    assert result.metrics.get("candidates_blocked_unverified", 0) >= 1
    prices = [c.get("price") for c in result.validated_candidates]
    assert 999.0 not in prices  # fabricated price blocked
    websites = [(c.get("website") or "") for c in result.validated_candidates]
    assert not any("shadow-supply" in w for w in websites)
    assert any("acme-chairs" in w for w in websites)  # grounded candidate survives


def test_controller_blocks_when_safe_would_cross_verify_via_other_vendor():
    """Shadow's fabricated 999 must not pass SAFE via Acme's page mentioning S$999."""
    from spider_qwen.agent.controller import Controller

    good = "https://acme-chairs.sg/ergonomic"
    bad = "https://shadow-supply.sg/ergonomic"
    fixtures = {
        good: {"title": "Acme Chairs", "text": "Acme supplies ergonomic office chairs in "
               "Singapore. Public pricing S$999 per unit. MOQ 50 units. Email sales@acme-chairs.sg."},
        bad: {"title": "Shadow Supply", "text": "Shadow Supply offers ergonomic office chairs "
              "in Singapore. Public pricing S$129 per unit. MOQ 50 units. Email sales@shadow-supply.sg."
              + _CROSS_VENDOR_SENTENCE},
    }
    controller = Controller(
        search_provider=_FixedSearch([good, bad]),
        fetch_provider=MockFetchProvider(fixtures=fixtures),
        qwen_json_extractor=_InjectingQwen("shadow-supply"),
        verify=True, state_dir=None, persist=False,
    )
    result = asyncio.run(controller.run("ergonomic office chairs Singapore",
                                        mode="product_exact_price"))
    assert result.metrics.get("candidates_blocked_unverified", 0) >= 1
    websites = [(c.get("website") or "") for c in result.validated_candidates]
    assert not any("shadow-supply" in w for w in websites)
    # Acme's own page lists S$999; only Shadow's injected 999 must be blocked.
    for cand in result.validated_candidates:
        if "acme-chairs" in (cand.get("website") or ""):
            assert cand.get("price") == 999.0


def test_controller_drops_unlocated_qwen_price_without_verification():
    # The Qwen price is quoted from text that is not on the page. It must not
    # reach output even with the spine off: no located span, no evidence row.
    controller, good, bad = _product_controller(verify=False)
    result = asyncio.run(controller.run("ergonomic office chairs Singapore",
                                        mode="product_exact_price"))
    prices = [c.get("price") for c in result.validated_candidates]
    assert 999.0 not in prices
    assert prices and all(price == 129.0 for price in prices)


def test_qwen_extraction_forces_verification_even_when_disabled():
    # A located, self-consistent quote that belongs to another vendor is caught
    # only by the spine, so a Qwen extraction seam must switch the spine on.
    controller, good, bad = _product_controller(verify=False, shadow_extra=_CROSS_VENDOR_SENTENCE)
    assert controller.verify_claims is True
    assert controller.verification_forced_by == ["qwen_structured_extraction"]
    result = asyncio.run(controller.run("ergonomic office chairs Singapore",
                                        mode="product_exact_price"))
    assert result.metrics.get("candidates_blocked_unverified", 0) >= 1
    assert result.metrics.get("verification_forced_by") == ["qwen_structured_extraction"]
    prices = [c.get("price") for c in result.validated_candidates]
    assert 999.0 not in prices
    assert 129.0 in prices


def test_verification_stays_optional_without_qwen_extraction():
    from spider_qwen.agent.controller import Controller

    controller = Controller(offline=True, verify=False, state_dir=None, persist=False)
    assert controller.verify_claims is False
    assert controller.verification_forced_by == []


def test_spine_blocks_candidate_with_only_noncritical_claims():
    ledger = EvidenceLedger("run_test", None)
    page_ref = ledger.record(source_tool="tinyfish_fetch", url="https://acme.sg",
                             snippet="Acme Pte Ltd", text="Acme Pte Ltd cleans offices.", metadata={})
    cand = SimpleNamespace(vendor_name="Acme Pte Ltd", pricing_status=PricingStatus.QUOTE_REQUIRED,
                           price=None, moq=None, evidence_refs=[page_ref])
    cv = VerificationSpine(ledger).verify_candidate(cand)
    assert all(not c.critical for c in cv.claims)
    assert cv.verified is False
    assert cv.unsupported_critical == ["no_critical_claims"]


def test_spine_never_grounds_claims_in_model_written_page_text():
    # Qwen web_extractor output is the model's rendering of a page. A price
    # "found" in it is model output checked against model output.
    ledger = EvidenceLedger("run_test", None)
    text = "Shadow Supply sells ergonomic chairs. Price S$999 per unit."
    page_ref = ledger.record(source_tool="qwen_web_extractor", url="https://shadow-supply.sg",
                             snippet=text, text=text, metadata={})
    claim_ref = ledger.record(source_tool="qwen_web_extractor", url="https://shadow-supply.sg",
                              snippet="S$999 per unit", text=None,
                              metadata={"field": "pricing", "claim_id": "claim_model_999",
                                        "parent_ledger_id": page_ref.ledger_id})
    cand = SimpleNamespace(vendor_name="Shadow Supply", price=999.0, currency="SGD", unit="unit",
                           moq=None, pricing_status=PricingStatus.EXACT_PRICE,
                           evidence_refs=[page_ref, claim_ref])
    cv = VerificationSpine(ledger).verify_candidate(cand)
    assert next(c for c in cv.claims if c.field == "price").verified is False
    assert cv.verified is False


# --- sentence boundaries vs dotted values ----------------------------------

def test_email_value_grounds_with_vendor_despite_dots_in_value():
    # A bare "." is not a sentence boundary: emails and domains contain dots,
    # and splitting inside them made email-valued relation claims (quote
    # channels) impossible to verify -- silently failing every one.
    page = ("Example Cleaning Pte Ltd accepts quotation requests at "
            "sales@example-cleaning.sg. Call us today.")
    res = MiniCheck().check(
        claim="Example Cleaning Pte Ltd accepts quotes at sales@example-cleaning.sg",
        value="sales@example-cleaning.sg", evidence_span=page,
        field="quote_channel", subject="Example Cleaning Pte Ltd",
    )
    assert res.supported, res.rationale


def test_decimal_price_grounds_in_one_sentence():
    page = "Acme Pte Ltd charges S$129.50 per unit. MOQ 50 units."
    res = MiniCheck().check(
        claim="Acme Pte Ltd charges 129.50", value="129.50", evidence_span=page,
        field="price", subject="Acme Pte Ltd",
    )
    assert res.supported, res.rationale
