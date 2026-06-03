import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from spider_qwen.evidence.ledger import EvidenceLedger
from spider_qwen.evidence.models import EvidenceRef, sha256_hex, utc_now_iso
from spider_qwen.evidence.verifier import verify_ledger
from spider_qwen.governance.audit import AuditLog, PolicyViolation
from spider_qwen.governance.review_events import ReviewStatusTransitionError, ReviewStore
from spider_qwen.memory.mcp import SemanticMemoryMcpAdapter
from spider_qwen.memory.semantic import SemanticFact, SemanticMemory
from spider_qwen.modes.contracts import ProcurementMode
from spider_qwen.modes.qwen_router import QwenModeRouter
from spider_qwen.tools.qwen_json_extractor import QwenJsonExtractor, QwenPageExtraction


class _FakeCompletions:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.payload


def test_qwen_json_provider_uses_json_object_mode():
    content = QwenPageExtraction().model_dump_json()
    completions = _FakeCompletions(
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    result = QwenJsonExtractor(client=client).extract(
        text="Request a quotation by emailing sales@example.sg.",
        page_url="https://example.sg",
        query="office cleaning Singapore quotation",
    )

    call = completions.calls[0]
    assert result.pricing.status == "NOT_FOUND"
    # DashScope-compatible: json_object (not OpenAI json_schema/strict), with the
    # schema supplied in the prompt and "json" present to satisfy the API.
    assert call["response_format"] == {"type": "json_object"}
    assert call["extra_body"] == {"enable_thinking": False}
    assert "json" in call["messages"][0]["content"].lower()
    assert "json schema" in call["messages"][1]["content"].lower()


def test_qwen_router_uses_tool_call_result():
    args = {"mode": "product_exact_price", "confidence": 0.82, "rationale": "price intent"}
    payload = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    tool_calls=[
                        SimpleNamespace(function=SimpleNamespace(arguments=json.dumps(args)))
                    ],
                    content=None,
                )
            )
        ]
    )
    completions = _FakeCompletions(payload)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))

    result = QwenModeRouter(client=client).classify("chairs price")

    assert result.mode == ProcurementMode.PRODUCT_EXACT_PRICE
    assert result.confidence == 0.82
    assert completions.calls[0]["tools"][0]["function"]["name"] == "select_procurement_mode"


def test_span_verifier_validates_child_claims():
    ledger = EvidenceLedger("run_test")
    text = "Request a quotation by emailing sales@example.sg."
    parent = ledger.record(source_tool="mock", url="https://example.sg", snippet=text, text=text)
    span = "sales@example.sg"
    start = text.index(span)
    child = ledger.record(
        source_tool="mock",
        url="https://example.sg",
        snippet=span,
        metadata={
            "claim_id": "claim_1",
            "parent_ledger_id": parent.ledger_id,
            "start_char": start,
            "end_char": start + len(span),
            "span_hash": sha256_hex(span),
        },
    )

    result = verify_ledger(ledger)

    assert result.ok
    assert result.checked_claims == 1
    assert child.ledger_id


def test_policy_violation_for_forbidden_audit_action():
    with pytest.raises(PolicyViolation):
        AuditLog("run_test").record("rfq_sent")


def test_review_store_round_trips(tmp_path):
    store = ReviewStore(tmp_path)
    event = store.create(run_id="run_1", reason="low confidence", proposed_action="use service")

    assert store.list(status="pending")[0].event_id == event.event_id
    approved = store.approve(event.event_id)
    assert approved is not None
    assert approved.status == "approved"


def test_review_status_is_terminal(tmp_path):
    store = ReviewStore(tmp_path)
    event = store.create(run_id="run_1", reason="rfq finalization", proposed_action="review")

    assert store.approve(event.event_id).status == "approved"
    assert store.approve(event.event_id).status == "approved"
    with pytest.raises(ReviewStatusTransitionError):
        store.reject(event.event_id)


def test_review_store_reject(tmp_path):
    store = ReviewStore(tmp_path)
    event = store.create(run_id="run_1", reason="disputed fact", proposed_action="review fact")

    rejected = store.reject(event.event_id)
    assert rejected is not None
    assert rejected.status == "rejected"
    assert store.list(status="pending") == []
    assert store.list(status="rejected")[0].event_id == event.event_id
    assert store.reject("missing") is None


def test_memory_maintenance_recall_and_mcp(tmp_path):
    memory = SemanticMemory(tmp_path)
    ref = EvidenceRef(
        ledger_id="ev_1",
        url="https://example.sg",
        snippet_hash="h",
        retrieved_at=utc_now_iso(),
    )
    active = memory.upsert(
        SemanticFact(
            entity_type="vendor",
            entity_name="Example Cleaning",
            field="quote_channel",
            value="sales@example.sg",
            confidence=0.9,
            evidence_refs=[ref],
        )
    )
    stale = memory.upsert(
        SemanticFact(
            entity_type="vendor",
            entity_name="Old Cleaning",
            field="quote_channel",
            value="old@example.sg",
            confidence=0.9,
            evidence_refs=[ref],
            last_verified_at=(datetime.now(timezone.utc) - timedelta(days=120)).isoformat(),
        )
    )

    assert memory.maintain(stale_days=90) == 1
    assert memory.get(stale.fact_id).status == "stale"
    recalls = memory.recall("Example Cleaning Singapore quotation", top_k=1, context_budget_chars=80)
    assert recalls[0].fact.fact_id == active.fact_id

    mcp = SemanticMemoryMcpAdapter(tmp_path, memory=memory)
    payload = mcp.call_tool(
        "semantic_memory.recall",
        {"query": "Example Cleaning quotation", "top_k": 1, "context_budget_chars": 80},
    )
    assert payload["recalls"][0]["fact"]["value"] == "sales@example.sg"


def test_ledger_load_rejects_path_traversal_id(tmp_path):
    # A caller-supplied run_id must never escape the state dir.
    for bad in ["../../etc/passwd", "..\\..\\secrets", "run/../../x", ""]:
        with pytest.raises(ValueError):
            EvidenceLedger.load(bad, tmp_path)
        with pytest.raises(ValueError):
            EvidenceLedger(bad, tmp_path)
    # Generated-style ids still load (empty ledger, no file yet).
    assert len(EvidenceLedger.load("run_abc123", tmp_path)) == 0


def test_assistant_config_ignored_for_git_and_docker():
    gitignore = open(".gitignore", encoding="utf-8").read().splitlines()
    dockerignore = open(".dockerignore", encoding="utf-8").read().splitlines()

    for ignored in [
        ".playwright-mcp/",
        ".playwright/",
        "playwright-report/",
        "test-results/",
        "tmppytest-cache/",
    ]:
        assert ignored in gitignore
        assert ignored in dockerignore
