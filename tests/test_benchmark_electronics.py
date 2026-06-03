"""T-8.1: obsolete-part S1/S2/S3 gold cases.

The gold set gains 20 electronics_substitution cases tagged by serendipity
sense (S1 substitute / S2 long-tail source / S3 risk signal). These assert the
deterministic, offline-testable contract: the queries classify as
electronics_substitution and flow through the harness without breaking the
evidence-coverage invariant. Deep S1/S2/S3 *slot content* is gated on the
deferred discovery-layer wiring (Phase 8 / T-8.2).
"""

from __future__ import annotations

import json
from pathlib import Path

from spider_qwen.benchmarks.evaluate_service_mode import run_gold_set
from spider_qwen.modes.classifier import ModeClassifier

GOLD_SET = "spider_qwen/benchmarks/gold_set.json"


def _electronics_cases():
    data = json.loads(Path(GOLD_SET).read_text(encoding="utf-8"))
    return [c for c in data["cases"] if c["expected_mode"] == "electronics_substitution"]


def test_twenty_obsolete_part_cases_exist():
    assert len(_electronics_cases()) == 20


def test_all_three_serendipity_senses_represented():
    senses = {c.get("serendipity_sense") for c in _electronics_cases()}
    assert senses == {"s1_substitute", "s2_long_tail_source", "s3_risk_signal"}


def test_every_electronics_case_classifies_correctly():
    classifier = ModeClassifier()
    for c in _electronics_cases():
        assert classifier.classify(c["query"]).mode.value == "electronics_substitution", c["case_id"]


def test_harness_reports_electronics_without_breaking_evidence_invariant():
    summary = run_gold_set(GOLD_SET, offline=True)
    assert summary["per_mode"]["electronics_substitution"]["cases"] == 20
    assert summary["per_mode"]["electronics_substitution"]["mode_classification_accuracy"] >= 0.9
    # Adding the new cases must not regress the cross-mode evidence invariant.
    assert summary["evidence_coverage"] >= 0.9
