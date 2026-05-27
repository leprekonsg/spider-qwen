"""End-to-end tests: CLI -> controller -> extraction -> RFQ -> persisted evidence.

These run the whole pipeline offline (deterministic mock providers) and verify
the externally observable contract: the CLI emits a well-formed result, the
evidence ledger is written to disk and reloadable via `evidence show`, traces
and audit logs are persisted, and the benchmark harness reports metrics.
"""

from __future__ import annotations

import json

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
    assert summary["cases"] == 6
    assert summary["mode_classification_accuracy"] >= 0.8
    assert summary["quote_channel_precision"] >= 0.9
    assert summary["rfq_draft_completeness"] >= 0.9
    assert summary["evidence_coverage"] >= 0.9


def test_benchmark_product_harness():
    summary = run_product_gold_set(GOLD_SET, offline=True)
    assert summary["cases"] == 2
    assert summary["mode_classification_accuracy"] == 1.0
    assert summary["pricing_status_accuracy"] == 1.0
    assert all(row["validated"] > 0 for row in summary["details"])
