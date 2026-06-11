"""End-to-end tests: CLI -> controller -> extraction -> RFQ -> persisted evidence.

These run the whole pipeline offline (deterministic mock providers) and verify
the externally observable contract: the CLI emits a well-formed result, the
evidence ledger is written to disk and reloadable via `evidence show`, traces
and audit logs are persisted, and the benchmark harness reports metrics.
"""

from __future__ import annotations

import json
import os

from spider_qwen.api.cli import main
from spider_qwen.benchmarks.evaluate_service_mode import run_gold_set
from spider_qwen.benchmarks.evaluate_product_mode import run_product_gold_set
from spider_qwen.evidence.ledger import EvidenceLedger

GOLD_SET = "spider_qwen/benchmarks/gold_set.json"


def _run_cli(capsys, argv: list[str]) -> dict:
    rc = main(argv)
    out = capsys.readouterr().out
    assert rc == 0, out
    return json.loads(out)


def test_cli_classify(capsys):
    payload = _run_cli(capsys, ["classify", "office cleaning Singapore"])
    assert payload["mode"] == "service_quote_required"


def test_cli_run_service_end_to_end(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("SPIDER_QWEN_STATE_DIR", str(tmp_path))
    result = _run_cli(capsys, ["run", "office cleaning Singapore", "--mode", "auto", "--offline"])

    assert result["mode"] == "service_quote_required"
    assert result["run_id"].startswith("run_")
    assert result["stop_reason"]
    assert result["rfq_drafts"], "service run should produce at least one RFQ draft"
    assert result["evidence_refs"], "validated candidates must carry evidence"

    # RFQ is a draft only -- never submitted/sent.
    for draft in result["rfq_drafts"]:
        joined = " ".join(draft["assumptions_and_limits"]).lower()
        assert "draft only" in joined
        assert draft["quote_channel"] is None or draft["quote_channel"]["evidence_ref"] is not None

    # Evidence persisted to disk and reloadable.
    ledger = EvidenceLedger.load(result["run_id"], state_dir=tmp_path)
    assert len(ledger) > 0
    quote_ref = result["rfq_drafts"][0]["quote_channel"]["evidence_ref"]["ledger_id"]
    quote_item = ledger.get(quote_ref)
    assert quote_item is not None
    assert quote_item.metadata.get("extraction") == "quote_channel"
    assert (tmp_path / "evidence" / f"{result['run_id']}.ledger.json").exists()
    assert (tmp_path / "traces" / f"{result['run_id']}.trace.json").exists()
    assert (tmp_path / "audit" / f"{result['run_id']}.audit.json").exists()
    assert (tmp_path / "memory" / "episodic.jsonl").exists()
    semantic_path = tmp_path / "memory" / "semantic.json"
    assert semantic_path.exists()
    facts = json.loads(semantic_path.read_text(encoding="utf-8"))
    assert facts and all(f["evidence_refs"] for f in facts)


def test_cli_evidence_show_round_trip(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("SPIDER_QWEN_STATE_DIR", str(tmp_path))
    run_payload = _run_cli(capsys, ["run", "pest control Singapore RFQ", "--offline"])
    items = _run_cli(capsys, ["evidence", "show", run_payload["run_id"]])
    assert isinstance(items, list) and items
    assert all("ledger_id" in item and "url" in item for item in items)


def test_cli_run_product_no_rfq(capsys, monkeypatch, tmp_path):
    monkeypatch.setenv("SPIDER_QWEN_STATE_DIR", str(tmp_path))
    result = _run_cli(capsys, ["run", "bulk printer paper supplier price per unit",
                               "--mode", "product_exact_price", "--offline"])
    assert result["mode"] == "product_exact_price"
    assert result["rfq_drafts"] == []
    assert result["validated_candidates"]
    assert result["pricing_status_summary"]
    assert all(c["pricing_status"] in {"EXACT_PRICE", "PRICE_RANGE", "STARTING_FROM", "RATE_CARD_FOUND"}
               for c in result["validated_candidates"])
    ledger = EvidenceLedger.load(result["run_id"], state_dir=tmp_path)
    price_ref = result["validated_candidates"][0]["evidence_refs"][-1]["ledger_id"]
    price_item = ledger.get(price_ref)
    assert price_item is not None
    assert price_item.metadata.get("extraction") == "pricing"


def test_benchmark_service_harness():
    summary = run_gold_set(GOLD_SET, offline=True)
    assert summary["cases"] == 100
    assert summary["per_mode"]["service_quote_required"]["cases"] == 20
    assert summary["per_mode"]["product_exact_price"]["cases"] == 20
    assert summary["per_mode"]["contact_enrichment_only"]["cases"] == 20
    assert summary["per_mode"]["revalidation"]["cases"] == 20
    assert summary["per_mode"]["electronics_substitution"]["cases"] == 20  # T-8.1 obsolete-part S1/S2/S3
    assert summary["mode_classification_accuracy"] >= 0.8
    assert summary["quote_channel_precision"] >= 0.9
    assert summary["rfq_draft_completeness"] >= 0.9
    assert summary["evidence_coverage"] >= 0.9


def test_benchmark_product_harness():
    summary = run_product_gold_set(GOLD_SET, offline=True)
    assert summary["cases"] == 20
    assert summary["mode_classification_accuracy"] >= 0.9
    assert summary["pricing_status_accuracy"] >= 0.8
    assert any(row["validated"] == 0 for row in summary["details"])  # missing/conflicting price hard-stops
    assert sum(1 for row in summary["details"] if row["validated"] > 0) >= 15


def test_cli_run_contact_enrichment(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("SPIDER_QWEN_STATE_DIR", str(tmp_path))
    result = _run_cli(
        capsys,
        ["run", "find contact email for Example Cleaning Pte Ltd Singapore",
         "--mode", "contact_enrichment_only", "--offline"],
    )
    assert result["mode"] == "contact_enrichment_only"
    assert result["rfq_drafts"] == []
    assert result["validated_candidates"]
    assert all(c["contacts"] for c in result["validated_candidates"])
    assert all(c["evidence_refs"] for c in result["validated_candidates"])


def test_cli_evidence_verify_and_graph(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("SPIDER_QWEN_STATE_DIR", str(tmp_path))
    run = _run_cli(capsys, ["run", "office cleaning Singapore", "--offline"])

    # Span-level evidence is independently re-derivable from the parent page text.
    # `evidence verify` exits 0 only when there are no issues (asserted by _run_cli).
    verify = _run_cli(capsys, ["evidence", "verify", run["run_id"]])
    assert verify["issues"] == []
    assert verify["checked_claims"] >= 1
    assert verify["valid_claims"] == verify["checked_claims"]

    # Supplier graph renders as Mermaid.
    rc = main(["evidence", "graph", run["run_id"]])
    graph_out = capsys.readouterr().out
    assert rc == 0
    assert graph_out.startswith("graph LR")


def test_cli_require_review_holds_and_releases_rfq(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("SPIDER_QWEN_STATE_DIR", str(tmp_path))
    result = _run_cli(capsys, ["run", "office cleaning Singapore", "--offline", "--require-review"])

    # With --require-review the polished RFQ is withheld behind a pending gate.
    assert result["rfq_drafts"], "expected held RFQ stubs"
    for stub in result["rfq_drafts"]:
        assert stub["status"] == "pending_review"
        assert "review_event_id" in stub
        assert "rfq_email_template" not in stub  # polished content withheld until approval
    assert result["metrics"]["held_for_review"] == len(result["rfq_drafts"])
    assert result["metrics"]["pending_reviews"] >= len(result["rfq_drafts"])

    # The run's own pending events are surfaced in the result, so API users
    # need no filesystem access to see what is waiting on a human.
    events = result["metrics"]["pending_review_events"]
    assert events
    assert all(e["event_id"] and e["proposed_action"] for e in events)
    assert any(e["reason"] == "rfq finalization" for e in events)

    pending = _run_cli(capsys, ["review", "list", "--status", "pending"])
    rfq_events = [e for e in pending if e["reason"] == "rfq finalization"]
    assert rfq_events
    event_id = rfq_events[0]["event_id"]
    assert rfq_events[0]["detail"]["rfq_draft"]["rfq_email_template"]

    # Approval releases the full polished draft carried in the event detail.
    approved = _run_cli(capsys, ["review", "approve", event_id])
    assert approved["status"] == "approved"
    assert approved["detail"]["rfq_draft"]["rfq_email_template"]


def test_cli_memory_show_and_cross_run_recall(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("SPIDER_QWEN_STATE_DIR", str(tmp_path))
    query = "example vendor cleaning Singapore quote"

    first = _run_cli(capsys, ["run", query, "--offline"])
    assert first["metrics"]["memory_recalls"] == 0  # nothing learned yet

    rows = _run_cli(capsys, ["memory", "show"])
    assert rows, "first run should persist evidence-backed semantic facts"
    assert all("decayed_confidence" in r and "is_stale" in r for r in rows)
    assert any(r["field"] == "quote_channel" for r in rows)
    assert all(r["evidence_refs"] for r in rows)

    # Second run recalls what the first learned (cross-run memory effect).
    semantic_path = tmp_path / "memory" / "semantic.json"
    ids_after_first = {f["fact_id"] for f in json.loads(semantic_path.read_text(encoding="utf-8"))}
    second = _run_cli(capsys, ["run", query, "--offline"])
    assert second["metrics"]["memory_recalls"] >= 1
    # The second run must never clobber what the first persisted.
    ids_after_second = {f["fact_id"] for f in json.loads(semantic_path.read_text(encoding="utf-8"))}
    assert ids_after_first <= ids_after_second


def test_cli_run_with_mock_qwen_json(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("SPIDER_QWEN_STATE_DIR", str(tmp_path))
    result = _run_cli(capsys, ["run", "office cleaning Singapore", "--offline", "--qwen-json"])
    assert result["mode"] == "service_quote_required"
    assert result["validated_candidates"]
    assert result["rfq_drafts"]

    # Qwen-enriched extraction still produces verifiable span evidence.
    verify = _run_cli(capsys, ["evidence", "verify", result["run_id"]])
    assert verify["issues"] == []
    assert verify["checked_claims"] >= 1


def test_cli_judged_demo_profile_enables_trust_surfaces(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("SPIDER_QWEN_STATE_DIR", str(tmp_path))
    for name in (
        "QWEN_STRUCTURED_EXTRACTION_ENABLED",
        "QWEN_ROUTER_FALLBACK_ENABLED",
        "QWEN_PAGE_JUDGE_ENABLED",
        "SPIDER_QWEN_VERIFICATION_ENABLED",
        "QWEN_NLI_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)

    result = _run_cli(capsys, [
        "run", "office cleaning Singapore", "--mode", "service_quote_required",
        "--offline", "--judged-demo",
    ])
    assert result["rfq_drafts"]
    assert result["serendipity"]["s3_risk_signals"] is not None
    assert result["metrics"]["verification_assessments"]
    draft = result["rfq_drafts"][0]
    assert draft["evidence_grade"] in {"high", "moderate", "low", "very_low"}
    assert draft["belief_interval"] is not None
    verify = _run_cli(capsys, ["evidence", "verify", result["run_id"]])
    assert verify["chain_ok"] is True
    assert "SPIDER_QWEN_VERIFICATION_ENABLED" not in os.environ


def test_offline_judged_demo_builds_no_live_qwen_clients(tmp_path, monkeypatch):
    # The dangerous combination: --offline --judged-demo with a real API key in
    # the env. offline is a guarantee -- the profile's flags must wire mocks or
    # heuristics, never a live DashScope client.
    import argparse

    from spider_qwen.api.cli import _apply_judged_demo_profile, _build_controller, _restore_env
    from spider_qwen.tools.qwen_json_extractor import MockQwenJsonExtractor

    monkeypatch.setenv("SPIDER_QWEN_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.delenv("TINYFISH_API_KEY", raising=False)  # offline needs no keys at all
    for name in (
        "QWEN_STRUCTURED_EXTRACTION_ENABLED",
        "QWEN_ROUTER_FALLBACK_ENABLED",
        "QWEN_PAGE_JUDGE_ENABLED",
        "SPIDER_QWEN_VERIFICATION_ENABLED",
        "QWEN_NLI_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)

    args = argparse.Namespace(offline=True, judged_demo=True, qwen_json=False,
                              serendipity=False, require_review=None)
    prior = _apply_judged_demo_profile(args)
    try:
        controller = _build_controller(args)
    finally:
        _restore_env(prior)

    assert controller.offline is True
    assert controller.qwen_router is None  # no live router fallback
    assert controller.minicheck is None  # spine stays on the deterministic heuristic
    assert isinstance(controller.qwen_json_extractor, MockQwenJsonExtractor)
    assert controller.page_judge is not None and controller.page_judge.llm is None
    assert controller.verify_claims is True  # the trust surface itself stays on


def test_controller_offline_is_self_sufficient(monkeypatch):
    # Direct construction, no injected providers, no keys: offline=True must
    # default to mock search/fetch instead of raising TinyFishError, and must
    # ignore env-driven live Qwen wiring.
    from spider_qwen.agent.controller import Controller
    from spider_qwen.tools.fetch_service import MockFetchProvider
    from spider_qwen.tools.search_service import MockSearchProvider

    monkeypatch.delenv("TINYFISH_API_KEY", raising=False)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("QWEN_ROUTER_FALLBACK_ENABLED", "1")
    monkeypatch.setenv("QWEN_NLI_ENABLED", "1")

    controller = Controller(offline=True, state_dir=None, persist=False)
    assert isinstance(controller.search_provider, MockSearchProvider)
    assert isinstance(controller.fetch_provider, MockFetchProvider)
    assert controller.qwen_router is None
    assert controller.minicheck is None


def test_cli_run_reason_uses_reasoning_spine(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("SPIDER_QWEN_STATE_DIR", str(tmp_path))
    result = _run_cli(
        capsys,
        ["run", "office cleaning Singapore", "--mode", "service_quote_required", "--offline", "--reason"],
    )
    # --reason routes through run_reasoning, so the payload is a ReasoningResult
    # (winner bundle + why-it-won explanation), not a RunResult.
    assert result["mode"] == "service_quote_required"
    assert result["winner"] is not None
    assert "won" in result["explanation"].lower()
    assert result["within_budget"] is True
    assert result["winner"]["trajectory"]["strategy"]
    assert result["winner"]["evidence_refs"], "winner bundle must carry ledger evidence"


def test_cli_run_default_is_not_reasoning(capsys, tmp_path, monkeypatch):
    # Without --reason the default run() pipeline is used (RunResult shape).
    monkeypatch.setenv("SPIDER_QWEN_STATE_DIR", str(tmp_path))
    result = _run_cli(capsys, ["run", "office cleaning Singapore", "--offline"])
    assert "winner" not in result
    assert "rfq_drafts" in result


def test_cli_evidence_rejects_path_traversal_id(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("SPIDER_QWEN_STATE_DIR", str(tmp_path))
    rc = main(["evidence", "show", "../../etc/passwd"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "invalid run_id" in err.lower()


def test_server_run_is_offline_only_by_default(tmp_path, monkeypatch):
    import pytest

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from spider_qwen.api.server import create_app

    monkeypatch.setenv("SPIDER_QWEN_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("SPIDER_QWEN_ALLOW_LIVE", raising=False)
    client = TestClient(create_app())

    # A client cannot switch on live web access.
    forced_live = client.post("/run", json={"query": "office cleaning Singapore", "offline": False})
    assert forced_live.status_code == 403

    # Offline runs work normally.
    offline = client.post("/run", json={"query": "office cleaning Singapore", "offline": True})
    assert offline.status_code == 200
    assert offline.json()["mode"] == "service_quote_required"

    invalid_mode = client.post("/run", json={
        "query": "office cleaning Singapore",
        "mode": "not_a_mode",
        "offline": True,
    })
    assert invalid_mode.status_code == 422
