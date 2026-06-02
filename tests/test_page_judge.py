"""T-2.1: Prometheus-style page judge gate.

Scores each fetched page on (relevance, freshness, source-authority,
contradicts-ledger) and returns accept | flag | reject. Deterministic heuristic;
an optional Qwen ``llm`` may override. Low-authority pages are flagged/rejected,
never silently stored.
"""

from __future__ import annotations

from spider_qwen.tools.page_judge import PageJudge


def test_authoritative_relevant_page_accepted():
    v = PageJudge(current_year=2026).judge(
        url="https://www.ti.com/product/LM358",
        title="LM358 Datasheet",
        text="LM358 dual operational amplifier. Updated 2025. View pricing and stock.",
        query="LM358 operational amplifier",
    )
    assert v.verdict == "accept"
    assert v.source_class == "manufacturer"
    assert v.authority >= 0.9
    assert v.relevance > 0.5


def test_marketplace_low_authority_rejected():
    v = PageJudge(current_year=2026).judge(
        url="https://www.aliexpress.com/item/12345.html",
        title="LM358 cheap lot",
        text="LM358 operational amplifier lot of 100, buy now 2025.",
        query="LM358 operational amplifier",
    )
    assert v.verdict == "reject"  # marketplace requires auth -> not silently stored
    assert v.source_class == "marketplace"
    assert v.authority <= 0.3


def test_offtopic_authoritative_page_rejected_on_relevance():
    v = PageJudge(current_year=2026).judge(
        url="https://www.ti.com/careers",
        title="Careers at TI",
        text="Join our team. Open roles in marketing and HR. Life at the company.",
        query="LM358 operational amplifier",
    )
    assert v.verdict == "reject"  # zero topical relevance
    assert v.relevance <= 0.1


def test_broker_page_flagged_not_rejected():
    v = PageJudge(current_year=2026).judge(
        url="https://www.rochesterelectronics.com/lm358",
        title="LM358 active stock",
        text="LM358 operational amplifier in stock. Request a quote. Pricing on request 2025.",
        query="LM358 operational amplifier",
    )
    assert v.verdict == "flag"  # broker authority -> usable but flagged
    assert v.source_class == "broker"


def test_stale_page_penalised_on_freshness():
    v = PageJudge(current_year=2026).judge(
        url="https://acme-components.sg/lm358",
        title="LM358",
        text="LM358 operational amplifier. Pricing and stock. Copyright 2008. Request a quote.",
        query="LM358 operational amplifier",
    )
    assert v.freshness <= 0.25
    assert v.verdict in {"flag", "reject"}


def test_contradiction_with_prior_ledger_item():
    class _Item:
        final_url = "https://acme-supply.sg/cleaning"
        url = final_url
        text = "Office cleaning Singapore. Price is S$10 per session."
        snippet = text

    v = PageJudge(current_year=2026).judge(
        url="https://acme-supply.sg/cleaning",
        title="Office cleaning",
        text="Office cleaning Singapore. Price is S$200 per session. Request a quote.",
        query="office cleaning Singapore",
        prior_items=[_Item()],
    )
    assert v.contradiction >= 0.4
    assert v.verdict == "flag"


def test_no_contradiction_when_prices_agree():
    class _Item:
        final_url = "https://acme-supply.sg/cleaning"
        url = final_url
        text = "Office cleaning Singapore. Price is S$200 per session."
        snippet = text

    v = PageJudge(current_year=2026).judge(
        url="https://acme-supply.sg/cleaning",
        title="Office cleaning",
        text="Office cleaning Singapore. Price is S$200 per session. Request a quote.",
        query="office cleaning Singapore",
        prior_items=[_Item()],
    )
    assert v.contradiction == 0.0


def test_verdict_is_deterministic():
    args = dict(url="https://www.mouser.sg/lm358", title="LM358",
                text="LM358 operational amplifier. In stock, pricing 2025.",
                query="LM358 operational amplifier")
    a = PageJudge(current_year=2026).judge(**args)
    b = PageJudge(current_year=2026).judge(**args)
    assert a.model_dump() == b.model_dump()


def test_llm_override_seam_is_used_when_provided():
    def fake_llm(prompt: str) -> dict:
        return {"verdict": "reject", "rationale": "llm override"}

    v = PageJudge(current_year=2026, llm=fake_llm).judge(
        url="https://www.ti.com/product/LM358",
        title="LM358 Datasheet",
        text="LM358 dual operational amplifier 2025 pricing stock.",
        query="LM358 operational amplifier",
    )
    assert v.verdict == "reject"
    assert "llm override" in v.rationale
