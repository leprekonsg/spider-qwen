from __future__ import annotations

from spider_qwen.extraction.quote_channel import QuoteChannelExtractor
from spider_qwen.modes.contracts import QuoteChannelType


def test_detects_contact_email():
    matches = QuoteChannelExtractor().extract("Email sales@vendor.sg for a quote.", [], "")
    assert any(m.type == QuoteChannelType.CONTACT_EMAIL and m.value == "sales@vendor.sg" for m in matches)


def test_detects_rfq_form_link():
    matches = QuoteChannelExtractor().extract("", ["https://vendor.sg/request-a-quote"], "")
    assert any(m.type == QuoteChannelType.RFQ_FORM for m in matches)


def test_detects_portal_login_required():
    matches = QuoteChannelExtractor().extract("Please login to view pricing.", [], "https://v.sg")
    assert any(m.type == QuoteChannelType.PORTAL_LOGIN_REQUIRED for m in matches)


def test_best_prefers_rfq_form_over_phone():
    ex = QuoteChannelExtractor()
    matches = ex.extract("Call +65 6123 4567", ["https://v.sg/rfq"], "")
    best = ex.best(matches)
    assert best is not None and best.type == QuoteChannelType.RFQ_FORM


def test_no_channel_returns_empty():
    matches = QuoteChannelExtractor().extract("We are a great company.", [], "")
    assert matches == []
