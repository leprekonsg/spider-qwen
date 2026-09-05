import asyncio

import pytest

from spider_qwen.api.factory import build_controller
from spider_qwen.evidence.ledger import EvidenceLedger
from spider_qwen.modes.contracts import ServiceCandidate
from spider_qwen.requirements import ProcurementRequest, assess_requirements, qualification


def _assess(text, *, requirement="overnight work", kind="mandatory", confirmed=True,
            source="mock", source_url="https://acme.sg/services"):
    ledger = EvidenceLedger("run_requirements")
    ref = ledger.record(source_tool=source, url=source_url, snippet=text, text=text)
    candidate = ServiceCandidate(vendor_name="Acme Cleaning", website="https://acme.sg", evidence_refs=[ref])
    request = ProcurementRequest(query="industrial cleaning in Tuas", requirements=[
        {"text": requirement, "kind": kind}], requirements_confirmed=confirmed,
        supplier_sources={"Acme Cleaning": ["https://acme.sg"]})
    assessments = assess_requirements(request, candidate, ledger)
    return assessments[0], qualification(request, assessments)


@pytest.mark.parametrize("text,status,outcome", [
    ("Acme Cleaning provides overnight work.", "supported", "qualified"),
    ("Acme Cleaning cannot provide overnight work.", "contradicted", "not_qualified"),
    ("We don't support overnight work.", "contradicted", "not_qualified"),
    ("We are reviewing overnight work.", "not_found", "unresolved"),
    ("We provide overnight work without extra fees.", "supported", "qualified"),
    ("Acme Cleaning provides industrial cleaning.", "not_found", "unresolved"),
    ("Acme Cleaning previously offered overnight work.", "not_found", "unresolved"),
    ("Acme Cleaning may provide overnight work.", "not_found", "unresolved"),
    ("Acme Cleaning asks: do you need overnight work?", "not_found", "unresolved"),
    ("Other Cleaning provides overnight work.", "not_found", "unresolved"),
    ("Acme Cleaning provides overnight work. Acme Cleaning cannot provide overnight work.", "not_found", "unresolved"),
])
def test_requirement_support_is_scoped_and_missing_is_not_satisfied(text, status, outcome):
    assessment, result = _assess(text)
    assert assessment.status == status
    assert result["status"] == outcome
    if status != "not_found":
        assert assessment.observed_at and assessment.evidence_refs and assessment.excerpts


def test_supported_fields_cannot_hide_unconfirmed_request_or_exclusion():
    _, incomplete = _assess("Acme Cleaning provides overnight work.", confirmed=False)
    assert incomplete["status"] == "unresolved"
    assert incomplete["unparsed_request"] == "industrial cleaning in Tuas"
    _, excluded = _assess("Acme Cleaning provides overnight work.", kind="exclusion")
    assert excluded["status"] == "not_qualified"
    _, absent = _assess("Acme Cleaning provides industrial cleaning.", kind="exclusion")
    assert absent["status"] == "unresolved"


@pytest.mark.parametrize("source,url", [
    ("semantic_memory", "https://acme.sg/services"),
    ("tinyfish_search", "https://acme.sg/services"),
    ("mock", "https://other.sg/services"),
])
def test_memory_search_and_other_supplier_evidence_cannot_qualify(source, url):
    assessment, result = _assess("Acme Cleaning provides overnight work.", source=source, source_url=url)
    assert assessment.status == "not_found"
    assert result["status"] == "unresolved"


def test_every_mandatory_requirement_survives_real_offline_pipeline(tmp_path, no_network):
    result = asyncio.run(build_controller(offline=True, state_dir=str(tmp_path)).run(
        "office cleaning Singapore", requirements=[
            {"text": "overnight work"}, {"text": "safety documentation"}, {"text": "Tuas"}],
        requirements_confirmed=True,
    ))
    assert not result.validated_candidates
    assert result.withheld_candidates
    assert result.stop_reason != "min_validated_candidates_met"
    for candidate in result.withheld_candidates:
        assessments = candidate["requirement_assessments"]
        assert {a["text"] for a in assessments} == {"overnight work", "safety documentation", "Tuas"}
        assert all(a["status"] == "not_found" for a in assessments)
        assert candidate["qualification"]["status"] == "unresolved"
        assert candidate["readiness"]["stage"] != "review_ready"
    assert result.qualification_summary["qualified_suppliers"] == 0
    assert result.procurement_request["requirements_confirmed"] is True
    assert not result.rfq_drafts


def test_requirements_persist_through_http_and_change_idempotency(tmp_path, no_network):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from spider_qwen.api.server import create_app
    from spider_qwen.application.run_service import RunService

    service = RunService(state_dir=tmp_path, controller_builder=build_controller)
    with TestClient(create_app(run_service=service)) as client:
        payload = {"query": "office cleaning Singapore", "profile": "offline_demo",
                   "requirements": [{"text": "overnight work"}], "requirements_confirmed": True,
                   "idempotency_key": "requirements-case"}
        started = client.post("/runs", json=payload)
        assert started.status_code == 202
        run_id = started.json()["run_id"]
        service.wait(run_id, owner="local")
        status = client.get(f"/runs/{run_id}").json()
        assert status["procurement_request"]["requirements"][0]["text"] == "overnight work"
        result = client.get(f"/runs/{run_id}/result").json()
        assert not result["validated_candidates"]
        assert result["withheld_candidates"][0]["requirement_assessments"][0]["status"] == "not_found"
        assert client.post("/runs", json=payload).json()["run_id"] == run_id
        payload["requirements"] = [{"text": "daytime work"}]
        assert client.post("/runs", json=payload).status_code == 409
    service.close()
    reopened = RunService(state_dir=tmp_path, controller_builder=build_controller)
    try:
        assert reopened.result(run_id, owner="local")["procurement_request"] == status["procurement_request"]
    finally:
        reopened.close()


def test_checklist_rejects_empty_and_duplicate_confirmation():
    with pytest.raises(ValueError, match="at least one"):
        ProcurementRequest(query="cleaning", requirements_confirmed=True)
    with pytest.raises(ValueError, match="unique"):
        ProcurementRequest(query="cleaning", requirements=[{"text": "overnight work"}] * 2)


def test_compound_phrase_and_source_attribution_are_explicit():
    assessment, result = _assess("We are certified for ISO 9001 and ISO 14001.", requirement="ISO 9001 and ISO 14001")
    assert assessment.status == "supported" and result["status"] == "qualified"
    ledger = EvidenceLedger("run_unproven_source")
    ref = ledger.record(source_tool="mock", url="https://directory.sg", text="Acme Cleaning provides overnight work.", snippet="Acme")
    candidate = ServiceCandidate(vendor_name="Acme Cleaning", website="https://directory.sg", evidence_refs=[ref])
    request = ProcurementRequest(query="cleaning", requirements=[{"text": "overnight work"}], requirements_confirmed=True)
    assessment = assess_requirements(request, candidate, ledger)[0]
    assert assessment.status == "not_found"
    assert assessment.reason == "supplier_source_ownership_unconfirmed"


def test_confirmed_supplier_assertions_qualify_through_real_search_fetch_pipeline(tmp_path, no_network):
    from spider_qwen.agent.controller import Controller
    from spider_qwen.tools.search_service import MockSearchProvider
    from spider_qwen.tools.fetch_service import MockFetchProvider

    class Search(MockSearchProvider):
        async def search(self, query, location, language, limit):
            return await super().search("fixture", location, language, limit)

    url = "https://acme.sg/cleaning"
    search = Search({"fixture": [{"url": url, "title": "Acme Cleaning", "snippet": "Office cleaning Singapore overnight work."}]})
    fetch = MockFetchProvider({url: {"title": "Acme Cleaning", "text": (
        "Acme Cleaning provides office cleaning in Singapore. "
        "We provide overnight work. We serve Tuas. We provide safety documentation. "
        "Acme Cleaning accepts quotation requests via sales@acme.sg. "
        "We provide commercial office cleaning services and prepare a quotation after a site survey."
    )}})
    controller = Controller(offline=True, state_dir=tmp_path, search_provider=search, fetch_provider=fetch)
    result = asyncio.run(controller.run("office cleaning Singapore", requirements=[
        {"text": "overnight work"}, {"text": "Tuas"}, {"text": "safety documentation"}],
        requirements_confirmed=True, supplier_sources={"Acme Cleaning": ["https://acme.sg"]}))
    assert len(result.validated_candidates) == 1
    candidate = result.validated_candidates[0]
    assert candidate["qualification"]["status"] == "qualified"
    assert result.qualification_summary["qualified_suppliers"] == 1
    assert result.rfq_drafts[0]["qualification"] == candidate["qualification"]
    assert result.rfq_drafts[0]["requirement_assessments"] == candidate["requirement_assessments"]
    assert candidate["readiness"]["stage"] != "review_ready"  # fixture, not independently verified
