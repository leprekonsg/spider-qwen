"""Trust signals surfaced where users see them, not just where they are computed.

Covers the four open items closed after the trust-layer review:
- GRADE + DS [Bel, Pl] on the RFQDraft itself,
- disputed-fact belief uncertainty as explicit S3 risk signals (tau-gated),
- the optional Qwen NLI scorer behind MiniCheck's model seam (guarded),
- the `evidence prove` CLI: inclusion proof, tamper demo, STH anchors.
"""

from __future__ import annotations

import json

import pytest

from spider_qwen.evidence.belief import UNCERTAINTY_TAU, quote_channel_interval
from spider_qwen.evidence.ledger import EvidenceLedger
from spider_qwen.evidence.models import EvidenceRef
from spider_qwen.memory.semantic import SemanticFact, SemanticMemory
from spider_qwen.modes.contracts import QuoteChannel, QuoteChannelType, ServiceCandidate
from spider_qwen.ranking.serendipity import disputed_fact_signals
from spider_qwen.rfq.generator import RFQGenerator
from spider_qwen.verification.minicheck import MiniCheck


def _ref(ledger_id: str = "ev_1", url: str = "https://a.sg") -> EvidenceRef:
    return EvidenceRef(ledger_id=ledger_id, url=url, snippet_hash="h",
                       retrieved_at="2026-01-01T00:00:00Z")


def _candidate(with_channel: bool = True) -> ServiceCandidate:
    qc = QuoteChannel(type=QuoteChannelType.CONTACT_EMAIL, value="sales@a.sg",
                      evidence_ref=_ref()) if with_channel else None
    return ServiceCandidate(
        vendor_name="Example Cleaning Pte Ltd", website="https://a.sg", country="Singapore",
        service_match_score=1.0, service_match_evidence=True, quote_channel=qc,
        evidence_refs=[_ref()],
    )


class _StubLedger:
    """Duck-typed ledger: .get(ledger_id) -> item carrying .reliability."""

    class _Item:
        def __init__(self, reliability: float) -> None:
            self.reliability = reliability

    def __init__(self, reliability: float) -> None:
        self._item = self._Item(reliability)

    def get(self, ledger_id: str):
        return self._item


# --- RFQDraft trust surface --------------------------------------------------

def test_rfq_draft_carries_grade_and_belief_interval():
    draft = RFQGenerator().generate(
        query="office cleaning Singapore", candidate=_candidate(True),
        target_country="Singapore", evidence_grade="moderate",
        belief_interval=quote_channel_interval(_candidate(True), _StubLedger(0.9)),
    )
    assert draft.evidence_grade == "moderate"
    assert draft.belief_interval is not None
    # Bel is the source's reliability mass; Pl = Bel + uncommitted remainder.
    assert draft.belief_interval.belief == pytest.approx(0.9)
    assert draft.belief_interval.plausibility == pytest.approx(1.0)
    # Stated in the draft's assumptions, not only in machine-readable fields.
    assert any("GRADE" in a for a in draft.assumptions_and_limits)
    assert any("[Bel, Pl]" in a for a in draft.assumptions_and_limits)


def test_rfq_draft_trust_fields_default_to_none_when_unverified():
    draft = RFQGenerator().generate(
        query="office cleaning Singapore", candidate=_candidate(True),
        target_country="Singapore",
    )
    assert draft.evidence_grade is None
    assert draft.belief_interval is None
    assert not any("GRADE" in a for a in draft.assumptions_and_limits)


def test_quote_channel_interval_none_without_channel():
    assert quote_channel_interval(_candidate(False)) is None


class _MemoryRowLedger:
    """Stub ledger resolving to a synthetic semantic_memory recall row."""

    class _Item:
        def __init__(self, confidence: float, source_urls: list[str]) -> None:
            self.source_tool = "semantic_memory"
            self.reliability = 0.6  # tier of the vendor page the recall attached to
            self.confidence = confidence
            self.metadata = {
                "source_evidence_refs": [{"url": u} for u in source_urls],
            }

    def __init__(self, confidence: float, source_urls: list[str]) -> None:
        self._item = self._Item(confidence, source_urls)

    def get(self, ledger_id: str):
        return self._item


def test_memory_backed_interval_uses_original_provenance_capped_by_recall():
    # Original provenance is a manufacturer page (tier 0.99) but the recall has
    # decayed to 0.6: the interval must honor the decay, not the synthetic
    # row's tier and not the original pedigree at full strength.
    interval = quote_channel_interval(
        _candidate(True), _MemoryRowLedger(0.6, ["https://www.ti.com/contact"]))
    assert interval.belief == pytest.approx(0.6)

    # Fresh recall (0.95) of a fact whose only original source is unclassified
    # (tier 0.4): the weak provenance caps the belief, not the recall freshness.
    interval = quote_channel_interval(
        _candidate(True), _MemoryRowLedger(0.95, ["https://random-blog.example/post"]))
    assert interval.belief == pytest.approx(0.4)


def test_memory_backed_interval_without_originals_uses_recall_confidence():
    interval = quote_channel_interval(_candidate(True), _MemoryRowLedger(0.7, []))
    # Not the synthetic row's 0.6 tier -- the recorded recall confidence.
    assert interval.belief == pytest.approx(0.7)


def test_quote_channel_interval_falls_back_to_half_without_ledger():
    interval = quote_channel_interval(_candidate(True), ledger=None)
    # QuoteChannel records no confidence; an unresolvable ref must not be
    # inflated beyond the 0.5 ignorance prior.
    assert interval.belief == pytest.approx(0.5)
    assert interval.plausibility == pytest.approx(1.0)


# --- DS uncertainty -> S3 risk signals ----------------------------------------

def _disputed_fact(conf_a: float, conf_b: float) -> SemanticFact:
    mem = SemanticMemory(None)
    mem.upsert(SemanticFact(
        entity_type="vendor", entity_name="Example Cleaning Pte Ltd",
        field="quote_channel", value="sales@a.sg", confidence=conf_a,
        evidence_refs=[_ref("ev_a", "https://a.sg")],
    ))
    fact = mem.upsert(SemanticFact(
        entity_type="vendor", entity_name="Example Cleaning Pte Ltd",
        field="quote_channel", value="sales@b.sg", confidence=conf_b,
        evidence_refs=[_ref("ev_b", "https://b.sg")],
    ))
    assert fact.status == "disputed"
    return fact


def test_high_conflict_dispute_yields_high_severity_signal():
    # 0.95 vs 0.9: pairwise conflict 0.855 > YAGER_CONFLICT_THRESHOLD -> Yager,
    # so the signal fires regardless of the gap and is high severity.
    signals = disputed_fact_signals([_disputed_fact(0.95, 0.9)])
    assert len(signals) == 1
    sig = signals[0]
    assert sig.signal_type == "belief_uncertainty"
    assert sig.severity == "high"
    assert "[Bel, Pl]" in sig.description and "sales@" in sig.description
    # Evidence from BOTH sides rides along -- the dispute is the evidence.
    assert {r.ledger_id for r in sig.evidence_refs} == {"ev_a", "ev_b"}


def test_wide_gap_low_conflict_dispute_yields_medium_signal():
    # 0.3 vs 0.3: Dempster keeps the rule, but over half the fused mass stays
    # uncommitted (uncertainty >= tau) -> medium severity.
    signals = disputed_fact_signals([_disputed_fact(0.3, 0.3)])
    assert len(signals) == 1
    assert signals[0].severity == "medium"


def test_confident_resolved_dispute_stays_quiet():
    # 0.9 vs 0.4: Dempster fuses to a committed verdict (narrow gap, low
    # conflict); flagging it would be noise.
    fact = _disputed_fact(0.9, 0.4)
    assert disputed_fact_signals([fact]) == []
    # And non-disputed facts are never fused at all.
    active = SemanticFact(entity_type="vendor", entity_name="X", field="quote_channel",
                          value="v", confidence=0.9, evidence_refs=[_ref()])
    assert disputed_fact_signals([active]) == []


def test_tau_is_documented_and_overridable():
    fact = _disputed_fact(0.3, 0.3)
    assert disputed_fact_signals([fact], tau=1.01) == []
    assert 0.0 < UNCERTAINTY_TAU < 1.0


def test_controller_flags_disputed_vendor_facts_into_s3(tmp_path):
    from spider_qwen.agent.controller import Controller
    from spider_qwen.governance.audit import AuditLog
    from spider_qwen.tools.fetch_service import MockFetchProvider
    from spider_qwen.tools.search_service import MockSearchProvider

    controller = Controller(search_provider=MockSearchProvider(),
                            fetch_provider=MockFetchProvider(),
                            state_dir=tmp_path, persist=True)
    memory = controller._semantic_memory()
    memory.upsert(SemanticFact(
        entity_type="vendor", entity_name="Example Cleaning Pte Ltd",
        field="quote_channel", value="sales@a.sg", confidence=0.95,
        evidence_refs=[_ref("ev_a")],
    ))
    memory.upsert(SemanticFact(
        entity_type="vendor", entity_name="Example Cleaning Pte Ltd",
        field="quote_channel", value="sales@b.sg", confidence=0.9,
        evidence_refs=[_ref("ev_b", "https://b.sg")],
    ))

    audit = AuditLog("run_t")
    ledger = EvidenceLedger("run_t")
    signals = controller._disputed_belief_signals(ledger, [_candidate(True)], audit)
    assert len(signals) == 1 and signals[0].severity == "high"
    assert any(e.action == "belief_uncertainty_flagged" for e in audit.events)

    # Disputes about vendors absent from this run are not this run's risks.
    other = ServiceCandidate(vendor_name="Unrelated Vendor", website="https://c.sg",
                             service_match_score=1.0, service_match_evidence=True,
                             evidence_refs=[_ref()])
    assert controller._disputed_belief_signals(ledger, [other], AuditLog("run_t2")) == []


# --- Qwen NLI scorer behind the MiniCheck seam --------------------------------

class _FakeQwenClient:
    def __init__(self, payload: str) -> None:
        self._payload = payload
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        payload = self._payload

        class _Msg:
            content = payload

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]

        return _Resp()


def test_qwen_nli_scorer_returns_score_and_rationale():
    from spider_qwen.verification.qwen_nli import QwenNliScorer

    scorer = QwenNliScorer(client=_FakeQwenClient('{"score": 0.9, "rationale": "entailed"}'))
    out = scorer("vendor accepts RFQs at sales@a.sg", "Contact sales@a.sg for quotations.")
    assert out == {"score": 0.9, "rationale": "entailed"}


def test_minicheck_guard_overrules_generous_qwen_score():
    from spider_qwen.verification.qwen_nli import QwenNliScorer

    scorer = QwenNliScorer(client=_FakeQwenClient('{"score": 0.99, "rationale": "looks fine"}'))
    mc = MiniCheck(model=scorer)
    # The value appears on the page but never co-located with the vendor (the
    # vendor line comes AFTER the value, so even the line-above price rule
    # cannot ground it): the co-location guard must overrule the model's 0.99.
    result = mc.check(
        claim="Acme Facilities quotes at sales@other.sg",
        value="sales@other.sg",
        evidence_span=("Different Vendor Pte Ltd: contact sales@other.sg for quotes.\n"
                       "Acme Facilities provides cleaning services."),
        subject="Acme Facilities",
    )
    assert result.supported is False
    assert result.method == "subject_ungrounded"


def test_minicheck_falls_back_when_qwen_scorer_raises():
    from spider_qwen.verification.qwen_nli import QwenNliScorer

    scorer = QwenNliScorer()  # no client, no key -> raises inside the seam
    mc = MiniCheck(model=scorer)
    result = mc.check(claim="value present", value="129",
                      evidence_span="The price is S$129 per unit.")
    # Heuristic verdict survives the model failure -- no crash, no silent flip.
    assert result.supported is True
    assert result.method == "value_grounded"


def test_policy_gates_qwen_nli_into_controller(tmp_path, monkeypatch):
    from spider_qwen.agent.controller import Controller
    from spider_qwen.tools.fetch_service import MockFetchProvider
    from spider_qwen.tools.search_service import MockSearchProvider
    from spider_qwen.verification.qwen_nli import QwenNliScorer

    def _controller():
        return Controller(search_provider=MockSearchProvider(),
                          fetch_provider=MockFetchProvider(),
                          state_dir=tmp_path, persist=False)

    monkeypatch.delenv("QWEN_NLI_ENABLED", raising=False)
    assert _controller().minicheck is None  # default: deterministic only

    monkeypatch.setenv("QWEN_NLI_ENABLED", "1")
    mc = _controller().minicheck
    assert isinstance(mc, MiniCheck)
    assert isinstance(mc.model, QwenNliScorer)


# --- evidence prove CLI --------------------------------------------------------

def _persisted_run(tmp_path) -> EvidenceLedger:
    ledger = EvidenceLedger("run_prove", state_dir=tmp_path)
    for i in range(3):
        ledger.record(source_tool="mock", url=f"https://example.com/{i}", snippet=f"s{i}")
    ledger.persist()
    return ledger


def test_evidence_prove_binds_proof_to_persisted_commitment(tmp_path, monkeypatch, capsys):
    from spider_qwen.api.cli import main

    ledger = _persisted_run(tmp_path)
    monkeypatch.delenv("SPIDER_QWEN_STH_SIGNING_KEY", raising=False)
    monkeypatch.setenv("SPIDER_QWEN_STATE_DIR", str(tmp_path))
    assert main(["evidence", "prove", "run_prove"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["proof_verified"] is True
    assert out["tamper_demo"]["proof_verified"] is False
    assert out["tree_head_published"] is True
    # The proof's head IS the persisted commitment (same timestamp), not a
    # recomputed one -- otherwise a persisted signature could never cover it.
    persisted = json.loads(ledger.path().read_text(encoding="utf-8"))
    assert out["citation_proof"]["tree_head"] == persisted["tree_head"]
    # No key at run time -> no persisted STH; absence is stated, never silent.
    assert "unavailable" in out["signed_tree_head"]


def test_evidence_prove_verifies_persisted_sth_against_operator_anchor(tmp_path, monkeypatch, capsys):
    pytest.importorskip("cryptography")
    from spider_qwen.api.cli import main
    from spider_qwen.evidence.transparency import generate_signing_key

    monkeypatch.setenv("SPIDER_QWEN_STH_SIGNING_KEY", generate_signing_key().hex())
    _persisted_run(tmp_path)  # persists WITH a signed tree head
    monkeypatch.setenv("SPIDER_QWEN_STATE_DIR", str(tmp_path))
    assert main(["evidence", "prove", "run_prove"]) == 0
    out = json.loads(capsys.readouterr().out)
    sth = out["signed_tree_head"]
    # The PERSISTED signature is emitted and verified against the trust anchor
    # derived from the operator's signing key -- not an ephemeral demo key.
    assert sth["sth"]["head"] == out["citation_proof"]["tree_head"]
    assert sth["verified_against_trust_anchor"] is True
    assert sth["verified_against_attacker_key"] is False


def test_run_result_ships_citation_proofs_bound_to_persisted_head(tmp_path):
    import asyncio

    from spider_qwen.agent.controller import Controller
    from spider_qwen.evidence.transparency import CitationProof, verify_citation

    controller = Controller(offline=True, state_dir=tmp_path)
    result = asyncio.run(controller.run("office cleaning services Singapore", mode="auto"))
    assert result.evidence_refs
    assert result.citation_proofs  # every final citation ships a proof bundle
    ledger_path = tmp_path / "evidence" / f"{result.run_id}.ledger.json"
    persisted = json.loads(ledger_path.read_text(encoding="utf-8"))
    for bundle in result.citation_proofs:
        proof = CitationProof.model_validate(bundle)
        assert verify_citation(proof)  # externally verifiable, no ledger access
        assert bundle["tree_head"] == persisted["tree_head"]


def test_evidence_prove_unknown_ledger_id_is_actionable(tmp_path, monkeypatch, capsys):
    from spider_qwen.api.cli import main

    _persisted_run(tmp_path)
    monkeypatch.setenv("SPIDER_QWEN_STATE_DIR", str(tmp_path))
    assert main(["evidence", "prove", "run_prove", "--ledger-id", "ev_missing"]) == 2
    err = capsys.readouterr().err
    assert "ev_missing" in err and "evidence show" in err
