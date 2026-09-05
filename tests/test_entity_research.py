from __future__ import annotations

import threading
import time
from collections import Counter

import pytest

from spider_qwen.application.entity_research import (
    DatasetConflict,
    DatasetNotFound,
    EntityResearchClaim,
    EntityResearchError,
    EntityResearchRunner,
    EntityResearchTask,
    EntityWorkContext,
)
from spider_qwen.evidence.models import EvidenceRef


def _ref(name: str) -> EvidenceRef:
    return EvidenceRef(
        ledger_id=f"ev_{name}",
        url=f"https://{name}.example/evidence",
        snippet_hash=f"hash_{name}",
        retrieved_at="2026-09-06T00:00:00Z",
    )


def _task(requirement_id: str, *, entity_id: str = "sup_a", payload=None, attempts=3):
    return EntityResearchTask(
        entity_id=entity_id,
        offering_id="ofr_a",
        requirement_id=requirement_id,
        input_payload=payload or {"query": requirement_id},
        max_attempts=attempts,
    )


def _claim(item, *, status="supported"):
    return EntityResearchClaim(
        entity_id=item.entity_id,
        offering_id=item.offering_id,
        requirement_id=item.requirement_id,
        status=status,
        value=f"value:{item.requirement_id}" if status != "not_found" else None,
        evidence_refs=[_ref(item.requirement_id)] if status != "not_found" else [],
        reason="test assertion",
    )


def _scope(owner="alice"):
    return {"owner": owner, "dataset_id": "shortlist", "input_version": "v1"}


def test_targeted_retry_preserves_completed_claims_and_attempts(tmp_path):
    calls = Counter()

    def handler(item, _context):
        calls[item.requirement_id] += 1
        if item.requirement_id == "req_retry" and calls[item.requirement_id] == 1:
            raise RuntimeError("temporary source failure")
        return _claim(item)

    with EntityResearchRunner(tmp_path, handler, max_concurrency=2) as runner:
        runner.submit(
            **_scope(),
            tasks=[_task("req_done"), _task("req_retry")],
            limits={"max_provider_calls": 2, "max_cost_micros": 20},
        )
        first = runner.run_pending(**_scope())
        assert first["work_items"] == {
            "queued": 0, "running": 0, "completed": 1, "failed": 1,
        }
        completed_before = runner.results(**_scope())[0].model_dump(mode="json")
        retry_item = next(
            item for item in runner.work_items(**_scope())
            if item.requirement_id == "req_retry"
        )

        queued = runner.retry_failed(**_scope(), work_ids=[retry_item.work_id])
        assert queued["retried"] == 1
        final = runner.run_pending(**_scope())
        assert final["work_items"]["completed"] == 2
        assert calls == {"req_done": 1, "req_retry": 2}
        assert completed_before in [claim.model_dump(mode="json") for claim in runner.results(**_scope())]
        attempts = {item.requirement_id: item.attempts for item in runner.work_items(**_scope())}
        assert attempts == {"req_done": 1, "req_retry": 2}


def test_submit_is_idempotent_but_rejects_changed_input_without_new_version(tmp_path):
    with EntityResearchRunner(tmp_path, lambda item, _ctx: _claim(item)) as runner:
        runner.submit(
            **_scope(), tasks=[_task("req_a", payload={"url": "https://a.example"})],
            limits={"max_provider_calls": 1, "max_cost_micros": 10},
        )
        repeated = runner.submit(
            **_scope(), tasks=[_task("req_a", payload={"url": "https://a.example"})],
            limits={"max_provider_calls": 1, "max_cost_micros": 10},
        )
        assert repeated["work_items"]["queued"] == 1
        with pytest.raises(DatasetConflict, match="changed without a new input_version"):
            runner.submit(
                **_scope(), tasks=[_task("req_a", payload={"url": "https://b.example"})],
                limits={"max_provider_calls": 1, "max_cost_micros": 10},
            )


def test_restart_requeues_running_item_without_refunding_reservation(tmp_path):
    first = EntityResearchRunner(tmp_path, lambda item, _ctx: _claim(item))
    first.submit(
        **_scope(), tasks=[_task("req_restart")],
        limits={"max_provider_calls": 2, "max_cost_micros": 10},
    )
    claimed = first._claim_next(**_scope())
    assert claimed is not None and claimed.attempts == 1
    EntityWorkContext(first, claimed).acquire(
        lambda: "provider returned before process crash",
        provider="mock_fetch",
        provider_calls=1,
        max_cost_micros=5,
    )
    first.close()

    def recovered_handler(item, context):
        return context.acquire(
            lambda: _claim(item),
            provider="mock_fetch",
            provider_calls=1,
            max_cost_micros=5,
        )

    with EntityResearchRunner(tmp_path, recovered_handler) as reopened:
        recovered = reopened.status(**_scope())
        assert recovered["work_items"]["queued"] == 1
        assert recovered["resources"]["provider_calls_reserved"] == 1
        assert recovered["resources"]["max_cost_micros_reserved"] == 5
        final = reopened.run_pending(**_scope())
        assert final["work_items"]["completed"] == 1
        assert final["resources"]["provider_calls_reserved"] == 2
        assert final["resources"]["max_cost_micros_reserved"] == 10
        assert reopened.work_items(**_scope())[0].attempts == 2


def test_atomic_dataset_quotas_hold_under_bounded_concurrency(tmp_path):
    active = 0
    peak = 0
    lock = threading.Lock()

    def handler(item, context):
        def operation():
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                time.sleep(0.04)
                return _claim(item)
            finally:
                with lock:
                    active -= 1

        return context.acquire(
            operation,
            provider="mock_fetch",
            provider_calls=1,
            max_cost_micros=10,
        )

    tasks = [_task(f"req_{index}") for index in range(5)]
    with EntityResearchRunner(tmp_path, handler, max_concurrency=3) as runner:
        runner.submit(
            **_scope(), tasks=tasks,
            limits={"max_provider_calls": 3, "max_cost_micros": 30},
        )
        status = runner.run_pending(**_scope())

        assert status["work_items"]["completed"] == 3
        assert status["work_items"]["failed"] == 2
        assert status["resources"]["provider_calls_reserved"] == 3
        assert status["resources"]["max_cost_micros_reserved"] == 30
        assert peak == 3
        assert all(
            "reservation would exceed" in (item.last_error or "")
            for item in runner.work_items(**_scope()) if item.status == "failed"
        )


def test_owner_scope_is_fixed_for_execution_and_reads(tmp_path):
    observed_owners = []

    def handler(item, context):
        observed_owners.append((item.owner, context.owner))
        return _claim(item, status="not_found")

    with EntityResearchRunner(tmp_path, handler) as runner:
        limits = {"max_provider_calls": 0, "max_cost_micros": 0}
        runner.submit(**_scope("alice"), tasks=[_task("req_a")], limits=limits)
        runner.submit(**_scope("bob"), tasks=[_task("req_a")], limits=limits)

        runner.run_pending(**_scope("alice"))
        assert runner.status(**_scope("alice"))["work_items"]["completed"] == 1
        assert runner.status(**_scope("bob"))["work_items"]["queued"] == 1
        assert runner.results(**_scope("bob")) == []
        with pytest.raises(DatasetNotFound):
            runner.status(**_scope("charlie"))

        runner.run_pending(**_scope("bob"))
        assert observed_owners == [("alice", "alice"), ("bob", "bob")]


def test_handler_cannot_complete_a_different_entity_claim(tmp_path):
    def handler(item, _context):
        claim = _claim(item)
        claim.entity_id = "sup_other"
        return claim

    with EntityResearchRunner(tmp_path, handler) as runner:
        runner.submit(
            **_scope(), tasks=[_task("req_a")],
            limits={"max_provider_calls": 0, "max_cost_micros": 0},
        )
        status = runner.run_pending(**_scope())
        assert status["work_items"]["failed"] == 1
        assert "does not match locked work item" in runner.work_items(**_scope())[0].last_error


def test_close_waits_for_workers_before_releasing_process_lock(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    def handler(item, _context):
        entered.set()
        assert release.wait(timeout=3)
        return _claim(item)

    runner = EntityResearchRunner(tmp_path, handler)
    runner.submit(
        **_scope(), tasks=[_task("req_a")],
        limits={"max_provider_calls": 0, "max_cost_micros": 0},
    )
    run_thread = threading.Thread(target=lambda: runner.run_pending(**_scope()))
    run_thread.start()
    assert entered.wait(timeout=2)

    close_thread = threading.Thread(target=runner.close)
    close_thread.start()
    time.sleep(0.05)
    assert close_thread.is_alive()
    with pytest.raises(EntityResearchError, match="closed"):
        runner.status(**_scope())
    with pytest.raises(RuntimeError, match="already owns"):
        EntityResearchRunner(tmp_path, handler)

    release.set()
    run_thread.join(timeout=3)
    close_thread.join(timeout=3)
    assert not run_thread.is_alive() and not close_thread.is_alive()
    with EntityResearchRunner(tmp_path, handler) as reopened:
        assert reopened.status(**_scope())["work_items"]["completed"] == 1


def test_stale_attempt_context_cannot_reserve_or_complete_retry(tmp_path):
    with EntityResearchRunner(tmp_path, lambda item, _context: _claim(item)) as runner:
        runner.submit(
            **_scope(), tasks=[_task("req_a")],
            limits={"max_provider_calls": 2, "max_cost_micros": 2},
        )
        stale = runner._claim_next(**_scope())
        assert stale is not None
        runner._fail(stale, RuntimeError("retry me"))
        runner.retry_failed(**_scope(), work_ids=[stale.work_id])
        current = runner._claim_next(**_scope())
        assert current is not None
        assert current.attempts == stale.attempts + 1

        with pytest.raises(EntityResearchError, match="resource reservation denied"):
            EntityWorkContext(runner, stale).acquire(
                lambda: None, provider="mock", provider_calls=1, max_cost_micros=1,
            )
        with pytest.raises(EntityResearchError, match="left running state"):
            runner._complete(stale, _claim(stale))
        assert runner.status(**_scope())["resources"]["provider_calls_reserved"] == 0
        runner._complete(current, _claim(current))
