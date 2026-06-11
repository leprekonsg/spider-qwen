"""T-7.3: cost router + cost dashboard.

Heuristic routing: extraction/classification/judge -> flash; planning/reasoning
-> max; the high_risk_procurement tag forces max. The dashboard emits $/run,
tokens, TinyFish calls, and $-saved-vs-all-max. Deterministic + offline: no LLM
runs in the offline pipeline, so token cost is 0 there, but the routing plan and
TinyFish call count are still logged.
"""

from __future__ import annotations

import json

from spider_qwen import SCHEMA_VERSION
from spider_qwen.agent.policy import load_policy
from spider_qwen.api.cli import main
from spider_qwen.observability.metrics import CostMeter, CostReport


# --- routing --------------------------------------------------------------

def test_flash_tasks_route_to_flash():
    pol = load_policy()
    for task in ("classification", "extraction", "judge"):
        d = pol.route_task(task)
        assert d.tier == "flash"
        assert d.model == pol.model_for("extraction")
        assert d.escalated is False


def test_planning_and_reasoning_route_to_max():
    pol = load_policy()
    for task in ("planning", "reasoning"):
        d = pol.route_task(task)
        assert d.tier == "max"
        assert d.model == pol.model_for("planner")


def test_high_risk_forces_max_for_decision():
    pol = load_policy()
    base = pol.route_task("decision")
    assert base.tier == "flash"  # a routine decision routes to flash
    forced = pol.route_task("decision", high_risk=True)
    assert forced.tier == "max"  # the high_risk_procurement tag forces max
    assert forced.model == pol.model_for("planner")
    assert forced.escalated is True


def test_high_risk_does_not_double_escalate_a_max_task():
    pol = load_policy()
    d = pol.route_task("planning", high_risk=True)
    assert d.tier == "max" and d.escalated is False  # already max -> not "escalated"


def test_unknown_task_defaults_to_flash():
    assert load_policy().route_task("brand_new_step").tier == "flash"


# --- pricing + dashboard math ---------------------------------------------

def test_cost_meter_computes_usd_tokens_and_savings():
    pol = load_policy()
    pricing = pol.model_pricing()
    flash, mx = pol.model_for("extraction"), pol.model_for("planner")
    meter = CostMeter()
    meter.record(flash, input_tokens=1000, output_tokens=1000)
    meter.record(mx, input_tokens=1000, output_tokens=1000)
    report = meter.report(pricing, max_model=mx, tinyfish_calls=7)
    assert report.tokens_total == 4000
    assert report.tinyfish_calls == 7
    assert report.total_usd > 0
    # flash tokens rebilled at the max price cost more -> strictly positive savings
    assert report.usd_saved_vs_all_max > 0
    assert {m.model for m in report.by_model} == {flash, mx}


def test_empty_meter_is_zero_cost():
    pol = load_policy()
    report = CostMeter().report(pol.model_pricing(), max_model=pol.model_for("planner"), tinyfish_calls=3)
    assert report.total_usd == 0.0
    assert report.tokens_total == 0
    assert report.tinyfish_calls == 3
    assert report.usd_saved_vs_all_max is None
    assert report.metering_status == "token metering unavailable in v1"
    assert report.schema_version == SCHEMA_VERSION
    assert isinstance(report, CostReport)


# --- controller / CLI integration -----------------------------------------

def _run(capsys, argv):
    rc = main(argv)
    out = capsys.readouterr().out
    assert rc == 0, out
    return json.loads(out)


def test_run_logs_cost_breakdown(capsys):
    cost = _run(capsys, ["run", "office cleaning Singapore", "--offline"])["metrics"]["cost"]
    assert "total_usd" in cost and "tokens_total" in cost
    assert cost["tinyfish_calls"] >= 1  # search + fetch happened
    assert "usd_saved_vs_all_max" in cost
    assert cost["metering_status"] == "token metering unavailable in v1"
    assert isinstance(cost["routing"], list) and cost["routing"]


def test_high_risk_run_routes_decision_to_max(capsys):
    hi = _run(capsys, ["run", "office cleaning Singapore", "--offline", "--high-risk"])
    lo = _run(capsys, ["run", "office cleaning Singapore", "--offline"])
    hi_routing = {r["task"]: r["tier"] for r in hi["metrics"]["cost"]["routing"]}
    lo_routing = {r["task"]: r["tier"] for r in lo["metrics"]["cost"]["routing"]}
    assert hi_routing["decision"] == "max"
    assert lo_routing["decision"] == "flash"
