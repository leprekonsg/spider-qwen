"""T-2.4: source-reliability prior + Merkle-DAG (hash-chained) ledger.

Acceptance: tampering with one ledger row breaks chain verification; the
source-reliability prior multiplies into per-claim (effective) confidence.
"""

from __future__ import annotations

import json

from spider_qwen.evidence.ledger import EvidenceLedger
from spider_qwen.governance.source_reliability import (
    DEFAULT_RELIABILITY,
    classify_source,
    reliability_for,
)


# --- source classification + reliability prior -----------------------------

def test_classify_source_tiers():
    assert classify_source("ti.com") == "manufacturer"
    assert classify_source("mouser.sg") == "distributor"
    assert classify_source("octopart.com") == "aggregator"
    assert classify_source("alibaba.com") == "marketplace"


def test_reliability_prior_ordering_matches_spec():
    mfr = reliability_for("https://ti.com/x")[1]
    dist = reliability_for("https://mouser.sg/x")[1]
    aggr = reliability_for("https://octopart.com/x")[1]
    mkt = reliability_for("https://alibaba.com/p")[1]
    assert mfr > dist > aggr > mkt
    assert mfr >= 0.99
    assert mkt <= 0.3


def test_reliability_overrides_apply():
    cls, prior = reliability_for("https://ti.com/x", priors={"manufacturer": 0.5})
    assert cls == "manufacturer" and prior == 0.5


# --- reliability multiplies into per-claim confidence ----------------------

def test_reliability_multiplies_into_effective_confidence():
    led = EvidenceLedger("run_rel")
    ref = led.record(source_tool="tinyfish_fetch", url="https://alibaba.com/p",
                     snippet="S$10", confidence=0.8)
    item = led.get(ref.ledger_id)
    assert item.reliability <= 0.3
    assert item.confidence == 0.8  # base confidence is not mutated
    assert abs(item.effective_confidence - 0.8 * item.reliability) < 1e-9


def test_manufacturer_outranks_marketplace_on_effective_confidence():
    led = EvidenceLedger("run_rel2")
    mfr = led.get(led.record(source_tool="tinyfish_fetch", url="https://ti.com/p",
                             snippet="x", confidence=0.8).ledger_id)
    mkt = led.get(led.record(source_tool="tinyfish_fetch", url="https://alibaba.com/p",
                             snippet="x", confidence=0.8).ledger_id)
    assert mfr.effective_confidence > mkt.effective_confidence


# --- Merkle-DAG chain ------------------------------------------------------

def test_chain_links_each_row_to_its_parent():
    led = EvidenceLedger("run_chain")
    r1 = led.record(source_tool="mock", url="https://a", snippet="one", confidence=0.5)
    r2 = led.record(source_tool="mock", url="https://b", snippet="two", confidence=0.5)
    i1, i2 = led.get(r1.ledger_id), led.get(r2.ledger_id)
    assert i1.parent_hash == ""  # genesis
    assert i2.parent_hash == i1.chain_hash
    assert i1.chain_hash and i2.chain_hash and i1.chain_hash != i2.chain_hash


def test_verify_chain_ok_for_untampered_ledger():
    led = EvidenceLedger("run_chain2")
    led.record(source_tool="mock", url="https://a", snippet="one", confidence=0.5)
    led.record(source_tool="mock", url="https://b", snippet="two", confidence=0.5)
    result = led.verify_chain()
    assert result.ok
    assert result.checked == 2
    assert result.issues == []


def test_chain_survives_persist_and_reload(tmp_path):
    led = EvidenceLedger("run_chain3", tmp_path)
    led.record(source_tool="mock", url="https://a", snippet="one", confidence=0.5)
    led.record(source_tool="mock", url="https://b", snippet="two", confidence=0.5)
    led.persist()
    reloaded = EvidenceLedger.load("run_chain3", tmp_path)
    assert reloaded.verify_chain().ok
    # A new append continues the chain off the reloaded tip.
    r3 = reloaded.record(source_tool="mock", url="https://c", snippet="three", confidence=0.5)
    assert reloaded.get(r3.ledger_id).parent_hash != ""
    assert reloaded.verify_chain().ok


def test_tampering_with_one_row_breaks_chain(tmp_path):
    led = EvidenceLedger("run_tamper", tmp_path)
    led.record(source_tool="mock", url="https://a", snippet="one", confidence=0.5)
    led.record(source_tool="mock", url="https://b", snippet="two", confidence=0.5)
    led.persist()

    path = led.path()
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["items"][0]["snippet"] = "ONE-TAMPERED"  # mutate content, leave chain_hash
    path.write_text(json.dumps(payload), encoding="utf-8")

    reloaded = EvidenceLedger.load("run_tamper", tmp_path)
    result = reloaded.verify_chain()
    assert not result.ok
    assert any(issue.ledger_id == payload["items"][0]["ledger_id"] for issue in result.issues)
