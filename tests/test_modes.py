from __future__ import annotations

from spider_qwen.modes.classifier import ModeClassifier
from spider_qwen.modes.contracts import ProcurementMode
from spider_qwen.modes.router import ModeRouter


def test_service_classification():
    result = ModeClassifier().classify("office cleaning Singapore")
    assert result.mode == ProcurementMode.SERVICE_QUOTE_REQUIRED


def test_product_classification():
    result = ModeClassifier().classify("500 ergonomic office chairs Singapore with public pricing")
    assert result.mode == ProcurementMode.PRODUCT_EXACT_PRICE


def test_contact_classification():
    result = ModeClassifier().classify("find contact email for Example Cleaning Pte Ltd")
    assert result.mode == ProcurementMode.CONTACT_ENRICHMENT_ONLY


def test_forced_mode_overrides():
    result = ModeClassifier().classify("anything", forced_mode="revalidation")
    assert result.mode == ProcurementMode.REVALIDATION
    assert result.confidence == 1.0


def test_router_service_produces_rfq():
    route = ModeRouter().route(ProcurementMode.SERVICE_QUOTE_REQUIRED)
    assert route.produces_rfq is True
    assert route.ranker == "service"


def test_router_product_no_rfq():
    route = ModeRouter().route(ProcurementMode.PRODUCT_EXACT_PRICE)
    assert route.produces_rfq is False
