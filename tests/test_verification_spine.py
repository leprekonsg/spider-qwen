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
from spider_qwen.modes.contracts import PricingStatus
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
                                          evidence_span="unrelated text")
    assert r.method == "model" and 0.0 <= r.score <= 1.0


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


def test_safe_search_fn_seam_supplies_grounding():
    mc = MiniCheck()
    claim = decompose(SimpleNamespace(vendor_name="Acme", price=129.0,
                                      pricing_status=PricingStatus.EXACT_PRICE,
                                      evidence_refs=[]))[1]
    res = SafeReverifier(mc, search_fn=lambda q: ["fresh result: Acme S$129 per unit"]).reverify(
        claim, corpus=[])
    assert res.supported


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


def test_spine_accepts_candidate_with_failed_noncritical_claim():
    # A failed non-critical claim (vendor name not in body) must not block a
    # candidate whose critical price claim is grounded.
    ledger = EvidenceLedger("run_test", None)
    page_ref, claim_ref = _record_page_and_claim(
        ledger, page_text="Pricing S$129 per unit. Ergonomic chairs in stock.",
        claim_value="S$129", grounded=True,
    )
    cand = SimpleNamespace(vendor_name="GhostVendor", price=129.0, currency="SGD", unit="unit",
                           moq=None, pricing_status=PricingStatus.EXACT_PRICE,
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


def _product_controller(verify):
    from spider_qwen.agent.controller import Controller
    good = "https://acme-chairs.sg/ergonomic"
    bad = "https://shadow-supply.sg/ergonomic"
    fixtures = {
        good: {"title": "Acme Chairs", "text": "Acme supplies ergonomic office chairs in "
               "Singapore. Public pricing S$129 per unit. MOQ 50 units. Email sales@acme-chairs.sg."},
        bad: {"title": "Shadow Supply", "text": "Shadow Supply offers ergonomic office chairs "
              "in Singapore. Public pricing S$129 per unit. MOQ 50 units. Email sales@shadow-supply.sg."},
    }
    controller = Controller(
        search_provider=_FixedSearch([good, bad]),
        fetch_provider=MockFetchProvider(fixtures=fixtures),
        qwen_json_extractor=_InjectingQwen("shadow-supply"),
        verify=verify, state_dir=None, persist=False,
    )
    return controller, good, bad


def test_controller_blocks_injected_unsupported_claim_from_output():
    controller, good, bad = _product_controller(verify=True)
    result = asyncio.run(controller.run("ergonomic office chairs Singapore",
                                        mode="product_exact_price"))
    assert result.metrics.get("candidates_blocked_unverified", 0) >= 1
    prices = [c.get("price") for c in result.validated_candidates]
    assert 999.0 not in prices  # fabricated price blocked
    websites = [(c.get("website") or "") for c in result.validated_candidates]
    assert not any("shadow-supply" in w for w in websites)
    assert any("acme-chairs" in w for w in websites)  # grounded candidate survives


def test_controller_without_verification_keeps_injected_claim():
    controller, good, bad = _product_controller(verify=False)
    result = asyncio.run(controller.run("ergonomic office chairs Singapore",
                                        mode="product_exact_price"))
    assert result.metrics.get("candidates_blocked_unverified", 0) == 0
    prices = [c.get("price") for c in result.validated_candidates]
    assert 999.0 in prices  # unverified -> fabricated price reaches output
    assert 129.0 in prices  # the grounded candidate also survives (asymmetry guard)
