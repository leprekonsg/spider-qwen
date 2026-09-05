import json

import pytest

from spider_qwen.application.entity_research import EntityResearchRunner, EntityResearchTask
from spider_qwen.application.requirement_research import (
    RequirementResearchHandler, RequirementResearchInput, main,
)
from spider_qwen.application.run_service import owner_state_dir
from spider_qwen.evidence.ledger import EvidenceLedger


class StubTinyFishClient:
    def __init__(self, response):
        self.response = response
        self.max_retries = 9
        self.calls = []
        self.closed = False

    async def fetch(self, urls, **_kwargs):
        self.calls.append({"urls": list(urls), "max_retries": self.max_retries})
        return self.response

    async def aclose(self):
        self.closed = True


def payload():
    return {
        "candidate_kind": "service",
        "candidate": {"supplier_id": "sup_acme", "offering_id": "off_cleaning",
                      "vendor_name": "Acme", "website": "https://acme.example",
                      "service_name": "cleaning"},
        "requirement": {"requirement_id": "req_night", "text": "overnight work"},
        "approved_origins": ["https://acme.example"],
        "urls": ["https://acme.example/services"],
    }


def test_refresh_cli_persists_claim_and_reopen_skips_fetch(tmp_path, capsys):
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"dataset_id": "d", "input_version": "v1",
                                  "limits": {"max_provider_calls": 1, "max_cost_micros": 0},
                                  "tasks": [payload()]}), encoding="utf-8")
    fixtures = tmp_path / "fixtures.json"
    fixtures.write_text(json.dumps({"https://acme.example/services": {
        "text": "We provide overnight work.", "title": "Acme services"}}), encoding="utf-8")
    args = [str(request), "--state-dir", str(tmp_path / "state"), "--fixtures", str(fixtures)]
    assert main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["results"][0]["status"] == "supported"
    assert first["results"][0]["evidence_refs"]
    # The second invocation has no usable page but must reuse the completed item.
    fixtures.write_text(json.dumps({"https://acme.example/services": {"status": 503}}), encoding="utf-8")
    assert main(args) == 0
    second = json.loads(capsys.readouterr().out)
    assert second == first
    assert second["status"]["resources"]["provider_calls_reserved"] == 1


def test_refresh_failed_page_retries_only_failed_work(tmp_path):
    data = RequirementResearchInput.model_validate(payload())
    task = EntityResearchTask(entity_id="sup_acme", offering_id="off_cleaning",
                              requirement_id="req_night", input_payload=data.model_dump(mode="json"))
    fixtures = {"https://acme.example/services": {"status": 503}}
    handler = RequirementResearchHandler(tmp_path, fixtures=fixtures)
    scope = dict(owner="alice", dataset_id="d", input_version="v1")
    with EntityResearchRunner(tmp_path, handler) as runner:
        runner.submit(**scope, tasks=[task], limits={"max_provider_calls": 2, "max_cost_micros": 0})
        assert runner.run_pending(**scope)["work_items"]["failed"] == 1
        fixtures["https://acme.example/services"] = {"text": "We do not provide overnight work."}
        runner.retry_failed(**scope)
        assert runner.run_pending(**scope)["work_items"]["completed"] == 1
        claim = runner.results(**scope)[0]
        assert claim.status == "contradicted"
        item = runner.work_items(**scope)[0]
        ledger = EvidenceLedger.load(f"research_{item.work_id}_{item.attempts}", owner_state_dir(tmp_path, "alice"))
        assert ledger.get(claim.evidence_refs[0].ledger_id).text == "We do not provide overnight work."
        assert runner.status(**scope)["resources"]["provider_calls_reserved"] == 2


@pytest.mark.parametrize("change", [
    {"urls": ["https://other.example/services"]},
    {"urls": ["https://user:password@acme.example/services"]},
    {"approved_origins": ["https://acme.example/services"]},
])
def test_refresh_rejects_unapproved_scope(change):
    with pytest.raises(ValueError):
        RequirementResearchInput.model_validate(payload() | change)


def test_refresh_does_not_import_old_candidate_evidence():
    data = RequirementResearchInput.model_validate(payload())
    assert data.parsed_candidate().evidence_refs == []


def test_live_refresh_requires_nonzero_upper_bound(tmp_path):
    with pytest.raises(ValueError, match="positive"):
        RequirementResearchHandler(tmp_path, offline=False)


def test_live_adapter_failure_consumes_one_reservation_without_internal_retry(
    tmp_path, monkeypatch,
):
    client = StubTinyFishClient({
        "results": [],
        "errors": [{"url": "https://acme.example/services", "status": 503,
                    "error": "HTTP 503"}],
    })
    monkeypatch.setattr("spider_qwen.tools.tinyfish_client.from_env", lambda: client)
    data = RequirementResearchInput.model_validate(payload())
    task = EntityResearchTask(
        entity_id="sup_acme", offering_id="off_cleaning", requirement_id="req_night",
        input_payload=data.model_dump(mode="json"),
    )
    scope = dict(owner="alice", dataset_id="d", input_version="v1")
    handler = RequirementResearchHandler(
        tmp_path, offline=False, max_cost_micros_per_fetch=17,
    )
    with EntityResearchRunner(tmp_path, handler) as runner:
        runner.submit(
            **scope, tasks=[task],
            limits={"max_provider_calls": 1, "max_cost_micros": 17},
        )
        status = runner.run_pending(**scope)
        assert status["work_items"]["failed"] == 1
        assert status["resources"]["provider_calls_reserved"] == 1
        assert status["resources"]["max_cost_micros_reserved"] == 17
        assert runner.results(**scope) == []
    assert client.calls == [{
        "urls": ["https://acme.example/services"], "max_retries": 0,
    }]
    assert client.closed


@pytest.mark.parametrize("final_url", [
    "https://other.example/services",
    "https://user:password@acme.example/services",
])
def test_live_adapter_redirect_cannot_bypass_approved_origin(
    tmp_path, monkeypatch, final_url,
):
    client = StubTinyFishClient({
        "results": [{
            "url": "https://acme.example/services", "final_url": final_url,
            "title": "Redirected", "text": "We provide overnight work.",
        }],
        "errors": [],
    })
    monkeypatch.setattr("spider_qwen.tools.tinyfish_client.from_env", lambda: client)
    data = RequirementResearchInput.model_validate(payload())
    task = EntityResearchTask(
        entity_id="sup_acme", offering_id="off_cleaning", requirement_id="req_night",
        input_payload=data.model_dump(mode="json"),
    )
    scope = dict(owner="alice", dataset_id="d", input_version="v1")
    with EntityResearchRunner(
        tmp_path,
        RequirementResearchHandler(
            tmp_path, offline=False, max_cost_micros_per_fetch=5,
        ),
    ) as runner:
        runner.submit(
            **scope, tasks=[task],
            limits={"max_provider_calls": 1, "max_cost_micros": 5},
        )
        status = runner.run_pending(**scope)
        assert status["work_items"]["failed"] == 1
        item = runner.work_items(**scope)[0]
        ledger = EvidenceLedger.load(
            f"research_{item.work_id}_{item.attempts}",
            owner_state_dir(tmp_path, "alice"),
        )
        assert ledger.items() == []


def test_cli_returns_failure_for_checkpointed_provider_error(tmp_path, capsys):
    request = tmp_path / "request.json"
    request.write_text(json.dumps({
        "dataset_id": "d", "input_version": "v1",
        "limits": {"max_provider_calls": 1, "max_cost_micros": 0},
        "tasks": [payload()],
    }), encoding="utf-8")
    fixtures = tmp_path / "fixtures.json"
    fixtures.write_text(json.dumps({
        "https://acme.example/services": {"status": 503},
    }), encoding="utf-8")

    assert main([
        str(request), "--state-dir", str(tmp_path / "state"),
        "--fixtures", str(fixtures),
    ]) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["status"]["work_items"]["failed"] == 1
    assert output["results"] == []


def test_retry_does_not_reuse_partial_evidence_from_failed_attempt(tmp_path):
    raw = payload()
    raw["urls"] = [
        "https://acme.example/first",
        "https://acme.example/second",
    ]
    data = RequirementResearchInput.model_validate(raw)
    task = EntityResearchTask(
        entity_id="sup_acme", offering_id="off_cleaning", requirement_id="req_night",
        input_payload=data.model_dump(mode="json"),
    )
    fixtures = {
        "https://acme.example/first": {"text": "We provide overnight work."},
        "https://acme.example/second": {"status": 503},
    }
    scope = dict(owner="alice", dataset_id="d", input_version="v1")
    with EntityResearchRunner(
        tmp_path, RequirementResearchHandler(tmp_path, fixtures=fixtures),
    ) as runner:
        runner.submit(
            **scope, tasks=[task],
            limits={"max_provider_calls": 4, "max_cost_micros": 0},
        )
        assert runner.run_pending(**scope)["work_items"]["failed"] == 1
        item = runner.work_items(**scope)[0]
        first_attempt = EvidenceLedger.load(
            f"research_{item.work_id}_1", owner_state_dir(tmp_path, "alice"),
        )
        first_attempt_ids = {e.ledger_id for e in first_attempt.items()}
        assert len(first_attempt_ids) == 1

        fixtures["https://acme.example/first"] = {"text": "General cleaning services."}
        fixtures["https://acme.example/second"] = {
            "text": "We do not provide overnight work.",
        }
        runner.retry_failed(**scope)
        assert runner.run_pending(**scope)["work_items"]["completed"] == 1
        claim = runner.results(**scope)[0]
        assert claim.status == "contradicted"
        assert first_attempt_ids.isdisjoint(ref.ledger_id for ref in claim.evidence_refs)
