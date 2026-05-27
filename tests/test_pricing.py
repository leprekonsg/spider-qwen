from __future__ import annotations

from spider_qwen.extraction.pricing import PricingExtractor
from spider_qwen.modes.contracts import PricingStatus


def _status(text: str) -> PricingStatus:
    return PricingExtractor().extract(text).status


def test_exact_price():
    result = PricingExtractor().extract("Our chairs are $10 per unit.")
    assert result.status == PricingStatus.EXACT_PRICE
    assert result.price == 10.0
    assert result.unit == "unit"


def test_price_range():
    assert _status("Pricing is $10-$20 per unit depending on volume.") == PricingStatus.PRICE_RANGE


def test_starting_from():
    assert _status("Plans from $99/month for managed support.") == PricingStatus.STARTING_FROM


def test_rate_card():
    assert _status("Download our rate card for full pricing.") == PricingStatus.RATE_CARD_FOUND


def test_quote_required():
    assert _status("Request a quotation for your project.") == PricingStatus.QUOTE_REQUIRED


def test_contact_for_pricing():
    assert _status("Contact sales for pricing and availability.") == PricingStatus.CONTACT_FOR_PRICING


def test_not_found():
    assert _status("We are a leading provider of office solutions.") == PricingStatus.NOT_FOUND


def test_conflicting():
    assert _status("Price is $10 each. Elsewhere it says $200 each.") == PricingStatus.CONFLICTING


def test_currency_normalized():
    result = PricingExtractor().extract("Each unit is S$25.")
    assert result.currency == "SGD"
