"""HTTP and MCP must project the same persisted procurement records."""

import time

import pytest

from spider_qwen.api.run_queries import CompletedRunQueries


def test_query_projections_keep_same_name_suppliers_and_drafts_separate():
    record = {
        "run_id": "run_example",
        "validated_candidates": [
            {"supplier_id": "a", "vendor_name": "同名", "website": "https://a.com.sg",
             "evidence_refs": [{"ledger_id": "ev_a"}], "conflicting_fields": ["country"]},
            {"supplier_id": "b", "vendor_name": "同名", "website": "https://b.com.sg",
             "evidence_refs": [{"ledger_id": "ev_b"}]},
        ],
        "rfq_drafts": [
            {"vendor": {"supplier_id": "b", "vendor_name": "同名"}, "rfq_email_template": "B draft"},
            {"vendor": {"supplier_id": "a", "vendor_name": "同名"}, "rfq_email_template": "A draft"},
        ],
        "citation_proofs": [{"ledger_id": "ev_a"}, {"ledger_id": "ev_b"}],
    }
    def load(run_id, *, owner):
        assert run_id == "run_example" and owner == "alice"
        return record
    queries = CompletedRunQueries(load)
    def inspect(operation, **kwargs):
        return queries.inspect("run_example", operation, owner="alice", **kwargs)
    evidence = inspect("get_candidate_evidence", supplier_id="a")
    assert evidence["evidence_refs"] == [{"ledger_id": "ev_a"}]
    assert evidence["citation_proofs"] == [{"ledger_id": "ev_a"}]
    assert evidence["conflicting_fields"] == ["country"]
    draft = inspect("get_rfq_draft", supplier_id="a")
    assert draft["submission_status"] == "unsent"
    assert draft["draft"]["rfq_email_template"] == "A draft"
    comparison = inspect("compare_candidates", supplier_ids=["b", "a"])
    assert [c["supplier_id"] for c in comparison["candidates"]] == ["b", "a"]
    comparison["candidates"][0]["vendor_name"] = "edited"
    assert record["validated_candidates"][1]["vendor_name"] == "同名"
    for ids in (["a", "a"], ["a"], ["a", "unknown"]):
        with pytest.raises(ValueError):
            inspect("compare_candidates", supplier_ids=ids)


def test_query_projections_require_offering_selector_for_multi_offering_supplier():
    record = {
        "run_id": "run_offerings",
        "validated_candidates": [
            {"supplier_id": "supplier-a", "offering_id": "paper", "vendor_name": "Office Co",
             "product_name": "A4 paper", "evidence_refs": [{"ledger_id": "ev_paper"}]},
            {"supplier_id": "supplier-a", "offering_id": "toner", "vendor_name": "Office Co",
             "product_name": "toner", "evidence_refs": [
                 {"ledger_id": "ev_toner"}, {"ledger_id": "ev_bulk"},
             ],
             "offer_scope_status": "multiple", "field_claims": {"offer_scope": [
                 {"value": {"item": "toner", "quantity": "1 cartridge", "price": 72.0},
                  "evidence_refs": [{"ledger_id": "ev_toner"}], "is_selected": True},
                 {"value": {"item": "toner", "quantity": "10 cartridges", "price": 650.0},
                  "evidence_refs": [{"ledger_id": "ev_bulk"}], "is_selected": False},
             ]}},
        ],
    }
    queries = CompletedRunQueries(lambda run_id, *, owner: record)

    with pytest.raises(ValueError, match="multiple offerings"):
        queries.inspect(
            "run_offerings", "get_candidate_evidence", owner="alice",
            supplier_id="supplier-a",
        )

    evidence = queries.inspect(
        "run_offerings", "get_candidate_evidence", owner="alice",
        supplier_id="supplier-a", offering_id="toner",
    )
    assert evidence["offering_id"] == "toner"
    assert evidence["evidence_refs"] == [
        {"ledger_id": "ev_toner"}, {"ledger_id": "ev_bulk"},
    ]
    assert evidence["offer_scope_status"] == "multiple"
    assert [claim["value"]["quantity"] for claim in evidence["offer_observations"]] == [
        "1 cartridge", "10 cartridges",
    ]

    comparison = queries.inspect(
        "run_offerings", "compare_candidates", owner="alice",
        offering_ids=["paper", "toner"],
    )
    assert [candidate["offering_id"] for candidate in comparison["candidates"]] == ["paper", "toner"]


def test_real_run_http_queries_match_mcp_and_result(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from spider_qwen.api.server import create_app
    from spider_qwen.mcp.handlers import inspect_run

    monkeypatch.setenv("SPIDER_QWEN_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("SPIDER_QWEN_PROFILE", "offline_demo")
    with TestClient(create_app()) as client:
        response = client.post("/runs", json={"query": "office cleaning Singapore"})
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        deadline = time.monotonic() + 30
        while True:
            status = client.get(f"/runs/{run_id}").json()
            if status["status"].lower() == "completed":
                break
            assert status["status"].lower() not in {"failed", "timed_out", "cancelled"}, status
            assert time.monotonic() < deadline, status
            time.sleep(0.02)
        result = client.get(f"/runs/{run_id}/result").json()
        def http(operation, args=None):
            response = client.post(f"/runs/{run_id}/inspect/{operation}", json=args or {})
            assert response.status_code == 200, response.text
            return response.json()
        candidates = http("list_candidates")["candidates"]
        assert candidates == result["validated_candidates"] and candidates
        assert all(c["supplier_id"] for c in candidates)
        operations = [("get_current_run", {}), ("list_candidates", {}),
                      ("get_candidate_evidence", {"supplier_id": candidates[0]["supplier_id"]}),
                      ("get_rfq_draft", {"supplier_id": candidates[0]["supplier_id"]})]
        if len(candidates) >= 2:
            operations.append(("compare_candidates", {"supplier_ids": [c["supplier_id"] for c in candidates[:2]]}))
        for operation, args in operations:
            assert http(operation, args) == inspect_run(run_id, operation, state_dir=str(tmp_path), **args)
        evidence = http("get_candidate_evidence", {"supplier_id": candidates[0]["supplier_id"]})
        assert evidence["observations"]
        assert {item["ledger_id"] for item in evidence["observations"]} == {
            ref["ledger_id"] for ref in candidates[0]["evidence_refs"]}
        assert client.post(f"/runs/{run_id}/inspect/get_rfq_draft", json={"supplier_id": "unknown"}).status_code == 422
        assert client.post(f"/runs/{run_id}/inspect/submit_rfq", json={}).status_code == 422
