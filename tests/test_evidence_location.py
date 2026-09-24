"""Evidence rows must be located in their parent page, never cite themselves.

An extracted value that is not on the fetched page (typically untrusted Qwen
output) used to be recorded with the value itself as the snippet and no
offsets, and verify_ledger skipped such rows.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys

from spider_qwen.evidence.ledger import EvidenceLedger
from spider_qwen.evidence.verifier import verify_ledger
from spider_qwen.modes.contracts import QuoteChannelType
from spider_qwen.tools.fetch_service import MockFetchProvider
from spider_qwen.tools.provider_types import SearchResult, SearchResultSet

_URL = "https://acme-cleaning.sg/office-cleaning"
_PAGE = (
    "Acme Cleaning Pte Ltd provides office cleaning in Singapore. "
    "Request a quotation by emailing sales@acme-cleaning.sg or call +65 6123 4567."
)


class _OneResultSearch:
    provider_name = "mock"
    search_source_tool = "mock"

    async def search(self, query, location, language, limit):
        results = [SearchResult(url=_URL, title="Acme Cleaning", snippet="office cleaning",
                                rank=0, source_tool="mock")]
        return SearchResultSet(query=query, location=location, results=results,
                               total_results=1, provider="mock")


class _FabricatingQwen:
    """Returns one channel and one contact that are on the page, one of each that is not."""

    def extract(self, *, text, page_url, query):
        from spider_qwen.tools.qwen_json_extractor import (
            QwenContactExtraction, QwenPageExtraction, QwenQuoteChannelExtraction,
        )
        return QwenPageExtraction(
            quote_channels=[
                # RFQ form outranks email in QuoteChannelExtractor.best, so an
                # unfiltered fabricated form would be the chosen channel.
                QwenQuoteChannelExtraction(type=QuoteChannelType.RFQ_FORM,
                                           value="https://evil.example/rfq"),
                QwenQuoteChannelExtraction(type=QuoteChannelType.CONTACT_EMAIL,
                                           value="sales@acme-cleaning.sg"),
            ],
            contacts=[
                QwenContactExtraction(type="email", value="ceo-private@evil.example"),
                QwenContactExtraction(type="email", value="sales@acme-cleaning.sg"),
            ],
        )


def _run(mode, tmp_path):
    from spider_qwen.agent.controller import Controller

    controller = Controller(
        search_provider=_OneResultSearch(),
        fetch_provider=MockFetchProvider(fixtures={_URL: {"title": "Acme Cleaning", "text": _PAGE}}),
        qwen_json_extractor=_FabricatingQwen(),
        verify=False, state_dir=str(tmp_path),
    )
    result = asyncio.run(controller.run("office cleaning Singapore", mode=mode))
    return result, EvidenceLedger.load(result.run_id, tmp_path)


def test_unlocated_qwen_quote_channel_never_reaches_output_or_ledger(tmp_path):
    result, ledger = _run("service_quote_required", tmp_path)
    channels = [(c.get("quote_channel") or {}).get("value") for c in result.validated_candidates]
    assert channels and "https://evil.example/rfq" not in channels
    assert all("evil.example" not in item.snippet for item in ledger.items())
    assert verify_ledger(ledger).ok


def test_unlocated_qwen_contact_never_reaches_output_or_ledger(tmp_path):
    result, ledger = _run("contact_enrichment_only", tmp_path)
    values = [c.get("value") for cand in result.validated_candidates for c in cand.get("contacts", [])]
    assert "sales@acme-cleaning.sg" in values
    assert "ceo-private@evil.example" not in values
    assert all("evil.example" not in item.snippet for item in ledger.items())
    assert verify_ledger(ledger).ok


def _page_row(ledger, links=()):
    return ledger.record(source_tool="tinyfish_fetch", url=_URL, snippet=_PAGE[:60],
                         text=_PAGE, metadata={"links": list(links)})


def test_verify_ledger_flags_extraction_row_without_location():
    ledger = EvidenceLedger("run_test", None)
    page = _page_row(ledger)
    ledger.record(source_tool="tinyfish_fetch", url=_URL, snippet="fabricated@evil.example",
                  metadata={"field": "quote_channel", "parent_ledger_id": page.ledger_id})
    result = verify_ledger(ledger)
    assert result.checked_claims == 1
    assert [issue.reason for issue in result.issues] == ["extraction row is not located in its parent"]


def test_verify_ledger_accepts_link_located_row_only_for_a_recorded_link():
    link = _URL + "/request-a-quote"
    ledger = EvidenceLedger("run_test", None)
    page = _page_row(ledger, links=[link])
    ledger.record(source_tool="tinyfish_fetch", url=_URL, snippet=link,
                  metadata={"field": "quote_channel", "parent_ledger_id": page.ledger_id,
                            "located_in": "links"})
    assert verify_ledger(ledger).ok

    ledger.record(source_tool="tinyfish_fetch", url=_URL, snippet=_URL + "/not-a-link",
                  metadata={"field": "quote_channel", "parent_ledger_id": page.ledger_id,
                            "located_in": "links"})
    assert not verify_ledger(ledger).ok


def test_verification_atomic_imports_on_its_own():
    completed = subprocess.run(
        [sys.executable, "-c", "import spider_qwen.verification.atomic"],
        capture_output=True, text=True,
    )
    assert completed.returncode == 0, completed.stderr
