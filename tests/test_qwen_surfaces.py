"""Qwen-centrality surfaces: shared factory, qwen_paths audit, trust verdicts,
CRAG corrective pivots (Yan et al. 2024), bounded verification replan, and the
CoVe-split RFQ drafter with deterministic fact-check (Dhuliawala et al. 2023).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from spider_qwen.agent.controller import Controller
from spider_qwen.api.cli import main
from spider_qwen.api.factory import build_controller
from spider_qwen.evidence.ledger import EvidenceLedger
from spider_qwen.rfq.factcheck import unsourced_numeric_claims
from spider_qwen.rfq.generator import RFQGenerator
from spider_qwen.rfq.qwen_drafter import MockQwenRfqDrafter, QwenRfqDraft
from spider_qwen.serendipity.corrective import CorrectiveVerdict, corrective_queries
from spider_qwen.serendipity.qwen_rewriter import MockQwenQueryRewriter


def _run_cli(capsys, argv: list[str]) -> dict:
    rc = main(argv)
    out = capsys.readouterr().out
    assert rc == 0, out
    return json.loads(out)


# --- shared factory ---------------------------------------------------------

def test_build_controller_offline_uses_mocks_everywhere():
    controller = build_controller(offline=True, qwen_json=True)
    assert controller.offline is True
    assert type(controller.search_provider).__name__ == "MockSearchProvider"
    assert type(controller.fetch_provider).__name__ == "MockFetchProvider"
    assert type(controller.qwen_json_extractor).__name__ == "MockQwenJsonExtractor"


# --- qwen_paths audit block ---------------------------------------------------

def test_default_offline_run_reports_honest_qwen_paths(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("SPIDER_QWEN_STATE_DIR", str(tmp_path))
    result = _run_cli(capsys, ["run", "office cleaning Singapore", "--offline"])
    paths = result["qwen_paths"]
    assert paths["offline"] is True
    # No flag enabled -> every Qwen seam honestly reports disabled.
    for seam in ("mode_router", "json_extractor", "nli_scorer",
                 "query_rewriter", "rfq_drafter"):
        assert paths[seam]["enabled"] is False, seam


def test_judged_demo_reports_mocked_seams_and_invocations(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("SPIDER_QWEN_STATE_DIR", str(tmp_path))
    result = _run_cli(capsys, ["run", "office cleaning Singapore", "--offline", "--judged-demo"])
    paths = result["qwen_paths"]
    assert paths["json_extractor"]["enabled"] is True
    assert paths["json_extractor"]["mock"] is True  # offline never implies live calls
    assert paths["json_extractor"]["invocations"] >= 1
    assert paths["query_rewriter"]["mock"] is True
    assert paths["rfq_drafter"]["mock"] is True


# --- trust verdicts -------------------------------------------------------------

def test_judged_demo_emits_composed_trust_verdicts(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("SPIDER_QWEN_STATE_DIR", str(tmp_path))
    result = _run_cli(capsys, ["run", "office cleaning Singapore", "--offline", "--judged-demo"])
    verdicts = result["trust_verdicts"]
    assert verdicts and len(verdicts) == len(result["validated_candidates"])
    v = verdicts[0]
    assert v["verification_enabled"] is True
    assert v["claims_verified"] >= 1
    assert v["grade"]
    assert v["decision"] == "proceed"
    assert v["belief_interval"] is not None
    # An uncalibrated emission gate must state the absence of a guarantee explicitly.
    assert v["conformal"]["calibrated"] is False
    assert v["conformal"]["risk_bound"] is None
    assert v["summary"].endswith(".")


def test_trust_verdicts_without_verification_say_so(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("SPIDER_QWEN_STATE_DIR", str(tmp_path))
    result = _run_cli(capsys, ["run", "office cleaning Singapore", "--offline"])
    verdicts = result["trust_verdicts"]
    assert verdicts
    assert verdicts[0]["verification_enabled"] is False
    assert "verification disabled" in verdicts[0]["summary"]


# --- CRAG corrective pivots ------------------------------------------------------

def test_corrective_queries_append_capped_qwen_pivots():
    verdict = CorrectiveVerdict(verdict="incorrect", mean_relevance=0.05)
    picks = corrective_queries("office cleaning Singapore", verdict,
                               mode="service_quote_required",
                               llm=MockQwenQueryRewriter())
    kinds = [p.kind for p in picks]
    assert kinds.count("qwen_pivot") == 2
    # Deterministic variants still lead; the model only appends.
    assert kinds[0] != "qwen_pivot"
    assert len({p.text.lower() for p in picks}) == len(picks)  # deduped


def test_corrective_queries_degrade_on_rewriter_failure():
    class Boom:
        def __call__(self, prompt: str) -> str:
            raise RuntimeError("rewriter down")

    verdict = CorrectiveVerdict(verdict="incorrect", mean_relevance=0.0)
    picks = corrective_queries("office cleaning Singapore", verdict, llm=Boom())
    assert picks  # deterministic variants survive
    assert all(p.kind != "qwen_pivot" for p in picks)


def test_reasoning_trace_surfaces_queries(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("SPIDER_QWEN_STATE_DIR", str(tmp_path))
    result = _run_cli(capsys, ["run", "office cleaning Singapore", "--offline"])
    reasoning = result["reasoning"]
    assert reasoning["initial_queries"]
    assert reasoning["crag"]["verdict"] in {"correct", "ambiguous", "incorrect"}
    assert reasoning["query_rewriter"] == "deterministic"


# --- bounded verification replan ---------------------------------------------------

class _TwoResultSearch:
    """Mock search returning 3 fixed vendor URLs for any query.

    Fixed URLs (not slug-based) ensure URL-level dedup collapses all search
    calls to exactly 3 unique pages, keeping candidates_extracted well under
    max_candidates_to_extract=10. Three distinct vendor domains also ensure
    min_validated_candidates=3 is met from SEA gather alone, so the geo-fallback
    does not consume the reserved search call — leaving budget for the replan.
    """

    provider_name = "mock"
    search_source_tool = "mock"
    rate_limited = False

    _VENDORS = [
        {"url": "https://example-vendor-1.sg/cleaning",
         "title": "Example Vendor 1 Pte Ltd - office cleaning Singapore",
         "snippet": "Provider 1 for office cleaning Singapore. Request a quotation via our contact page."},
        {"url": "https://example-vendor-2.sg/cleaning",
         "title": "Example Vendor 2 Pte Ltd - office cleaning Singapore",
         "snippet": "Provider 2 for office cleaning Singapore. Request a quotation via our contact page."},
        {"url": "https://example-vendor-3.sg/cleaning",
         "title": "Example Vendor 3 Pte Ltd - office cleaning Singapore",
         "snippet": "Provider 3 for office cleaning Singapore. Request a quotation via our contact page."},
    ]

    def __init__(self) -> None:
        pass

    async def search(self, query, location, language, limit):
        from spider_qwen.tools.provider_types import SearchResult, SearchResultSet
        results = [
            SearchResult(url=v["url"], title=v["title"], snippet=v["snippet"],
                         rank=i, source_tool="mock")
            for i, v in enumerate(self._VENDORS)
        ]
        return SearchResultSet(query=query, location=location, provider="mock",
                               results=results, total_results=len(results))


def test_replan_runs_exactly_one_bounded_round(monkeypatch):
    controller = Controller(offline=True, state_dir=None, persist=False, verify=True,
                            search_provider=_TwoResultSearch())
    calls = {"n": 0}
    real = controller._verify_candidates

    def fake(ledger, validated, tracer):
        calls["n"] += 1
        if calls["n"] == 1:
            # First pass: everything blocked with the spine recommending replan.
            return [], {"claims_verified": 0, "claims_unsupported": len(validated),
                        "candidates_blocked_unverified": len(validated),
                        "verification_assessments": {},
                        "replan_recommended": True}
        return real(ledger, validated, tracer)

    monkeypatch.setattr(controller, "_verify_candidates", fake)
    result = asyncio.run(controller.run("office cleaning Singapore",
                                        mode="service_quote_required"))
    assert calls["n"] == 2  # exactly one corrective re-verify, never a loop
    assert result.metrics["replan_rounds"] == 1
    assert result.reasoning["replan_queries"]


def test_zero_claim_candidate_recommends_replan():
    controller = Controller(offline=True, state_dir=None, persist=False, verify=True)
    ledger = EvidenceLedger("run_replan_signal")

    class Ghost:
        # No vendor name and no extractable fields -> decompose() yields zero
        # claims -> the spine fails closed with decision "replan".
        vendor_name = ""
        evidence_refs: list = []

    kept, metrics = controller._verify_candidates(ledger, [Ghost()], None)
    assert kept == []
    assert metrics["replan_recommended"] is True


class _SixResultSearch(_TwoResultSearch):
    _VENDORS = [
        {
            "url": f"https://reserve-vendor-{i}.sg/cleaning",
            "title": f"Reserve Vendor {i} Pte Ltd - office cleaning Singapore",
            "snippet": "Office cleaning Singapore. Request a quotation via our contact page.",
        }
        for i in range(1, 9)
    ]


def test_verification_refills_from_fetched_reserve_before_research(monkeypatch):
    """The sixth fetched supplier is checked before a corrective search spends.

    The first visible batch is rejected.  Three lower-ranked candidates already
    fetched in the same run pass verification, meeting the output minimum without
    invoking the one allowed verification-replan search round.
    """
    controller = Controller(
        offline=True, state_dir=None, persist=False, verify=True,
        search_provider=_SixResultSearch(),
    )
    batches: list[int] = []

    def fake_verify(ledger, batch, tracer):
        batches.append(len(batch))
        if len(batch) > 1:
            return [], {
                "claims_verified": 0,
                "claims_unsupported": len(batch),
                "candidates_blocked_unverified": len(batch),
                "verification_assessments": {},
                "replan_recommended": True,
            }
        return list(batch), {
            "claims_verified": 1,
            "claims_unsupported": 0,
            "candidates_blocked_unverified": 0,
            "verification_assessments": {},
            "replan_recommended": False,
        }

    monkeypatch.setattr(controller, "_verify_candidates", fake_verify)
    result = asyncio.run(controller.run("office cleaning Singapore", mode="service_quote_required"))
    assert batches[0] == 5
    assert batches[1:] == [1, 1, 1]
    assert result.metrics["reserve_candidates_verified"] == 3
    assert len(result.validated_candidates) == 3
    assert result.reasoning["replan_queries"] == []


class _ConsolidatedSupplierSearch:
    provider_name = "mock"
    search_source_tool = "mock"
    rate_limited = False

    def __init__(self, rows):
        self._rows = rows

    async def search(self, query, location, language, limit):
        from spider_qwen.tools.provider_types import SearchResult, SearchResultSet

        return SearchResultSet(
            query=query,
            provider="mock",
            total_results=len(self._rows),
            results=[SearchResult(rank=index, source_tool="mock", **row)
                     for index, row in enumerate(self._rows)],
        )


def test_controller_withholds_consolidated_pricing_conflict_from_rfq():
    """A merged supplier with incompatible pricing states never reaches RFQ output."""
    from spider_qwen.tools.fetch_service import MockFetchProvider

    first = "https://origin.example/services"
    second = "https://origin.example/contact"
    rows = [
        {"url": first, "title": "Origin Exterminators Pte Ltd",
         "snippet": "Pest control Singapore. Request a quotation."},
        {"url": second, "title": "Origin Exterminators Pte Ltd | Contact",
         "snippet": "Pest control Singapore. Contact sales."},
    ]
    fetch = MockFetchProvider(fixtures={
        first: {
            "title": rows[0]["title"],
            "text": "Origin Exterminators Pte Ltd provides pest control across Singapore. "
                    "Pest control service is S$100 per visit. Request a quotation at sales@origin.example.",
            "links": [],
        },
        second: {
            "title": rows[1]["title"],
            "text": "Origin Exterminators Pte Ltd provides pest control across Singapore. "
                    "Request a quotation at sales@origin.example.",
            "links": [],
        },
    })
    result = asyncio.run(Controller(
        offline=True, persist=False,
        search_provider=_ConsolidatedSupplierSearch(rows), fetch_provider=fetch,
    ).run("pest control Singapore", mode="service_quote_required"))

    assert result.validated_candidates == []
    assert result.rfq_drafts == []
    assert result.metrics["candidates_considered"] == 1
    assert result.serendipity["primary_answer"] is None
    assert len(result.withheld_candidates) == 1
    withheld = result.withheld_candidates[0]
    assert withheld["withheld_reason"] == "unresolved conflicting field claim"
    assert "pricing_status" in withheld["withheld_fields"]
    claims = withheld["field_claims"]["pricing_status"]
    assert {claim["value"] for claim in claims} == {"EXACT_PRICE", "QUOTE_REQUIRED"}
    assert all(claim["evidence_refs"] for claim in claims)


def test_controller_recomputes_completeness_for_complementary_supplier_pages():
    """Service evidence and a quote channel from separate pages form one supplier."""
    from spider_qwen.tools.fetch_service import MockFetchProvider

    service = "https://origin.example/services"
    contact = "https://origin.example/contact"
    rows = [
        {"url": service, "title": "Origin Exterminators Pte Ltd",
         "snippet": "Pest control Singapore."},
        {"url": contact, "title": "Origin Exterminators Pte Ltd | Contact",
         "snippet": "Contact Origin Exterminators."},
    ]
    result = asyncio.run(Controller(
        offline=True, persist=False,
        search_provider=_ConsolidatedSupplierSearch(rows),
        fetch_provider=MockFetchProvider(fixtures={
            service: {
                "title": rows[0]["title"],
                "text": "Origin Exterminators Pte Ltd provides pest control across Singapore.",
                "links": [],
            },
            contact: {
                "title": rows[1]["title"],
                "text": "Origin Exterminators Pte Ltd. Request a quotation at sales@origin.example.",
                "links": [],
            },
        }),
    ).run("pest control Singapore", mode="service_quote_required"))

    assert result.metrics["candidates_considered"] == 1
    assert len(result.validated_candidates) == 1
    candidate = result.validated_candidates[0]
    assert candidate["evidence_completeness"] == 1.0
    assert len(candidate["evidence_refs"]) >= 2
    assert len(result.rfq_drafts) == 1


# --- RFQ drafter + deterministic fact-check ------------------------------------------

def test_unsourced_numeric_claims_flags_only_ungrounded_numbers():
    body = "Our rate is S$50 per visit with a 10% surcharge, replied within 5 business days."
    corpus = "Cleaning services at S$50 per visit across Singapore."
    flags = unsourced_numeric_claims(body, corpus)
    assert "S$50" not in flags
    assert "10%" in flags
    assert any("5 business days" in f for f in flags)
    assert unsourced_numeric_claims(body, body) == []


def _service_candidate():
    from spider_qwen.evidence.models import EvidenceRef, sha256_hex, utc_now_iso
    from spider_qwen.modes.contracts import QuoteChannel, QuoteChannelType, ServiceCandidate

    ref = EvidenceRef(ledger_id="ev_test", url="https://example.sg",
                      snippet_hash=sha256_hex("sales@example.sg"),
                      retrieved_at=utc_now_iso())
    return ServiceCandidate(
        vendor_name="Example Cleaning Pte Ltd",
        website="https://example.sg",
        country="Singapore",
        quote_channel=QuoteChannel(type=QuoteChannelType.CONTACT_EMAIL,
                                   value="sales@example.sg", evidence_ref=ref),
        service_match_evidence=True,
        evidence_completeness=1.0,
    )


def test_generator_uses_drafter_and_flags_unsourced_claims():
    class FakeDrafter:
        model = "fake-qwen"

        def draft(self, **kwargs):
            return QwenRfqDraft(
                body="Dear team, we expect pricing near S$99 and delivery in 3 days.",
                language="en-SG-neutral",
            )

    generator = RFQGenerator(minimum_completeness=0.0, drafter=FakeDrafter())
    draft = generator.generate(query="office cleaning Singapore",
                               candidate=_service_candidate(), evidence_corpus="")
    assert draft.drafted_by == "qwen:fake-qwen"
    assert draft.language == "en-SG-neutral"
    assert "S$99" in draft.unsourced_claims
    assert any("S$99" in a for a in draft.assumptions_and_limits)


def test_generator_falls_back_to_template_on_drafter_failure():
    class Broken:
        model = "broken"

        def draft(self, **kwargs):
            raise RuntimeError("model down")

    generator = RFQGenerator(minimum_completeness=0.0, drafter=Broken())
    draft = generator.generate(query="office cleaning Singapore",
                               candidate=_service_candidate())
    assert draft.drafted_by == "template"
    assert draft.rfq_email_template  # deterministic body survived
    assert draft.unsourced_claims == []
    assert any("template" in a for a in draft.assumptions_and_limits)


def test_mock_drafter_body_passes_its_own_factcheck():
    drafter = MockQwenRfqDrafter()
    drafted = drafter.draft(query="office cleaning Singapore",
                            vendor_name="Example Cleaning Pte Ltd",
                            country="Singapore")
    assert unsourced_numeric_claims(drafted.body, "") == []


def test_judged_demo_rfq_drafts_are_qwen_drafted(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("SPIDER_QWEN_STATE_DIR", str(tmp_path))
    result = _run_cli(capsys, ["run", "office cleaning Singapore", "--offline", "--judged-demo"])
    drafts = [d for d in result["rfq_drafts"] if d.get("rfq_email_template")]
    assert drafts
    assert all(d["drafted_by"] == "qwen:mock" for d in drafts)
    assert all(d["language"] == "en-SG-neutral" for d in drafts)
    assert all(d["unsourced_claims"] == [] for d in drafts)  # mock invents nothing
