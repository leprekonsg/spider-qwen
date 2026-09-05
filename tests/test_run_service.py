from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

import pytest

from spider_qwen.application.run_service import (
    AdmissionRejected,
    IdempotencyConflict,
    RunNotFound,
    RunNotReady,
    RunService,
    load_run_result,
)


class _Tracker:
    def __init__(self, delay: float = 0.08):
        self.delay = delay
        self.active = 0
        self.peak = 0
        self.built: list[dict] = []
        self.lock = threading.Lock()

    def builder(self, **config):
        self.built.append(config)
        tracker = self

        class Controller:
            async def run(self, query, *, mode, target_country, high_risk, serendipity, run_id):
                with tracker.lock:
                    tracker.active += 1
                    tracker.peak = max(tracker.peak, tracker.active)
                try:
                    await asyncio.sleep(5 if query == "block" else tracker.delay)
                    return {"run_id": run_id, "query": query, "mode": mode,
                            "validated_candidates": [], "rfq_drafts": []}
                finally:
                    with tracker.lock:
                        tracker.active -= 1

        return Controller()


def _wait_for(service, run_id, owner="local", wanted=None, timeout=3):
    wanted = wanted or {"completed", "failed", "cancelled", "timed_out", "interrupted"}
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        status = service.status(run_id, owner=owner)
        if status["status"] in wanted:
            return status
        time.sleep(0.01)
    raise AssertionError(f"run did not reach {wanted}: {service.status(run_id, owner=owner)}")


def test_run_service_idempotency_owner_scope_and_read_only_loader(tmp_path):
    tracker = _Tracker()
    service = RunService(state_dir=tmp_path, controller_builder=tracker.builder)
    try:
        request = {"query": "chairs", "profile": "offline_demo"}
        first = service.start(request, owner="alice", idempotency_key="same")
        duplicate = service.start(request, owner="alice", idempotency_key="same")
        assert duplicate["run_id"] == first["run_id"]
        with pytest.raises(IdempotencyConflict):
            service.start({"query": "desks", "profile": "offline_demo"},
                          owner="alice", idempotency_key="same")
        with pytest.raises(RunNotFound):
            service.status(first["run_id"], owner="bob")

        _wait_for(service, first["run_id"], owner="alice")
        result = load_run_result(tmp_path, first["run_id"], owner="alice")
        assert result["run_id"] == first["run_id"]
        assert result["profile"] == "offline_demo"
        assert result["effective_config"]["provider_mode"] == "mock"
        assert len(result["effective_config"]["config_fingerprint"]) == 64
        assert "models" in result["effective_config"]["policy"]
        assert result["execution"]["distributed_scheduler"] is False
        with pytest.raises(RunNotFound):
            load_run_result(tmp_path, first["run_id"], owner="bob")
    finally:
        service.close()


def test_run_service_cancellation_is_acknowledged_by_worker(tmp_path):
    service = RunService(state_dir=tmp_path, controller_builder=_Tracker().builder)
    try:
        started = service.start({"query": "block", "profile": "offline_demo"}, owner="local")
        _wait_for(service, started["run_id"], wanted={"running"})
        requested = service.cancel(started["run_id"], owner="local")
        assert requested["status"] in {"cancelling", "cancelled"}
        final = _wait_for(service, started["run_id"])
        assert final["status"] == "cancelled"
        kinds = [event["kind"] for event in service.events(started["run_id"], owner="local")]
        assert "cancellation_requested" in kinds
        assert "cancellation_acknowledged" in kinds
    finally:
        service.close()


def test_cancel_during_controller_build_never_enters_run(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    executed = threading.Event()

    def builder(**_config):
        entered.set()
        assert release.wait(timeout=2)

        class Controller:
            async def run(self, *_args, **_kwargs):
                executed.set()
                await asyncio.sleep(0)
                return {}

        return Controller()

    service = RunService(state_dir=tmp_path, controller_builder=builder)
    try:
        started = service.start({"query": "barrier", "profile": "offline_demo"}, owner="local")
        assert entered.wait(timeout=2)
        service.cancel(started["run_id"], owner="local")
        release.set()
        assert _wait_for(service, started["run_id"])["status"] == "cancelled"
        assert not executed.is_set()
    finally:
        release.set()
        service.close()


def test_run_service_bounds_global_concurrency_and_queue(tmp_path):
    tracker = _Tracker(delay=0.2)
    service = RunService(
        state_dir=tmp_path, controller_builder=tracker.builder,
        max_concurrency=1, max_queued=1,
    )
    try:
        first = service.start({"query": "block", "profile": "offline_demo"}, owner="local")
        _wait_for(service, first["run_id"], wanted={"running"})
        second = service.start({"query": "queued", "profile": "offline_demo"}, owner="local")
        assert second["status"] == "queued"
        with pytest.raises(AdmissionRejected):
            service.start({"query": "overflow", "profile": "offline_demo"}, owner="local")
        service.cancel(first["run_id"], owner="local")
        _wait_for(service, first["run_id"])
        _wait_for(service, second["run_id"])
        assert tracker.peak == 1
    finally:
        service.close()


def test_daily_live_admission_cap_is_persisted(tmp_path):
    service = RunService(
        state_dir=tmp_path, controller_builder=_Tracker(delay=0.01).builder,
        allow_live=True, max_live_runs_per_utc_day=1,
    )
    try:
        first = service.start({"query": "first", "profile": "live_research"}, owner="alice")
        _wait_for(service, first["run_id"], owner="alice")
        with pytest.raises(AdmissionRejected, match="Daily live-run"):
            service.start({"query": "second", "profile": "live_research"}, owner="bob")
    finally:
        service.close()


def test_restart_marks_abandoned_run_interrupted(tmp_path):
    tracker = _Tracker()
    service = RunService(state_dir=tmp_path, controller_builder=tracker.builder)
    started = service.start({"query": "quick", "profile": "offline_demo"}, owner="local")
    _wait_for(service, started["run_id"])
    service.close()
    with sqlite3.connect(tmp_path / "runs.sqlite3") as conn:
        conn.execute(
            "UPDATE runs SET status='running',finished_at=NULL,error=NULL WHERE run_id=?",
            (started["run_id"],),
        )
    recovered = RunService(state_dir=tmp_path, controller_builder=tracker.builder)
    try:
        status = recovered.status(started["run_id"], owner="local")
        assert status["status"] == "interrupted"
        assert recovered.events(started["run_id"], owner="local")[-1]["kind"] == "interrupted"
    finally:
        recovered.close()


def test_second_service_cannot_recover_live_owner_database(tmp_path):
    tracker = _Tracker()
    first = RunService(state_dir=tmp_path, controller_builder=tracker.builder)
    try:
        running = first.start({"query": "block", "profile": "offline_demo"}, owner="local")
        _wait_for(first, running["run_id"], wanted={"running"})
        with pytest.raises(RuntimeError, match="already owns"):
            RunService(state_dir=tmp_path, controller_builder=tracker.builder)
        assert first.status(running["run_id"], owner="local")["status"] == "running"
        first.cancel(running["run_id"], owner="local")
        _wait_for(first, running["run_id"])
    finally:
        first.close()


def test_http_profiles_auth_ownership_idempotency_and_legacy(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from spider_qwen.api.server import create_app

    monkeypatch.setenv("SPIDER_QWEN_ALLOW_LIVE", "1")
    monkeypatch.setenv(
        "SPIDER_QWEN_API_TOKENS",
        '{"secret-a":"alice","secret-b":"bob","secret-local":"local"}',
    )
    tracker = _Tracker(delay=0.03)
    service = RunService(
        state_dir=tmp_path, controller_builder=tracker.builder, allow_live=True,
        max_live_concurrency=1,
    )
    headers_a = {"Authorization": "Bearer secret-a"}
    headers_b = {"Authorization": "Bearer secret-b"}
    try:
        with TestClient(create_app(run_service=service)) as client:
            config = client.get("/config").json()
            assert {p["name"] for p in config["profiles"]} == {
                "offline_demo", "live_research", "reviewed_procurement",
            }
            unauthenticated = client.post("/runs", json={
                "query": "live", "profile": "live_research",
            })
            assert unauthenticated.status_code == 401

            local_run = client.post(
                "/runs", json={"query": "private local", "offline": True},
                headers={"Authorization": "Bearer secret-local"},
            ).json()
            _wait_for(service, local_run["run_id"], owner="local")
            assert client.get(f"/runs/{local_run['run_id']}").status_code == 401
            assert client.get(f"/runs/{local_run['run_id']}/result").status_code == 401
            assert client.post(
                f"/runs/{local_run['run_id']}/inspect/get_current_run", json={},
            ).status_code == 401

            body = {"query": "demo", "offline": True, "idempotency_key": "web-key"}
            first = client.post("/runs", json=body, headers=headers_a)
            duplicate = client.post("/runs", json=body, headers=headers_a)
            assert first.status_code == 202
            assert duplicate.json()["run_id"] == first.json()["run_id"]
            run_id = first.json()["run_id"]
            assert client.get(f"/runs/{run_id}", headers=headers_b).status_code == 404
            _wait_for(service, run_id, owner="alice")
            assert client.get(f"/runs/{run_id}/result", headers=headers_a).status_code == 200
            events = client.get(f"/runs/{run_id}/events", headers=headers_a).json()["events"]
            assert events[-1]["kind"] == "completed"

            reviewed = client.post("/runs", json={
                "query": "reviewed", "profile": "reviewed_procurement",
            }, headers=headers_a)
            assert reviewed.status_code == 202
            _wait_for(service, reviewed.json()["run_id"], owner="alice")
            assert tracker.built[-1]["verify"] is True
            assert tracker.built[-1]["require_review"] is True

            cancellable = client.post("/runs", json={"query": "block", "offline": True},
                                      headers=headers_a).json()
            _wait_for(service, cancellable["run_id"], owner="alice", wanted={"running"})
            cancelled = client.post(f"/runs/{cancellable['run_id']}/cancel", headers=headers_a)
            assert cancelled.status_code == 202
            assert _wait_for(service, cancellable["run_id"], owner="alice")["status"] == "cancelled"

            legacy = client.post(
                "/run", json={"query": "legacy", "offline": True}, headers=headers_a,
            )
            assert legacy.status_code == 200
            assert legacy.json()["query"] == "legacy"
    finally:
        service.close()


def test_http_real_offline_controller_lifecycle(tmp_path, monkeypatch, no_network):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from spider_qwen.api.factory import build_run_service
    from spider_qwen.api.server import create_app

    monkeypatch.delenv("SPIDER_QWEN_ALLOW_LIVE", raising=False)
    monkeypatch.delenv("SPIDER_QWEN_API_TOKENS", raising=False)
    service = build_run_service(state_dir=str(tmp_path), max_concurrency=1, max_queued=0)
    try:
        with TestClient(create_app(run_service=service)) as client:
            started = client.post("/runs", json={
                "query": "office cleaning Singapore", "profile": "offline_demo",
            })
            assert started.status_code == 202
            run_id = started.json()["run_id"]
            final = _wait_for(service, run_id, timeout=5)
            assert final["status"] == "completed"
            result = client.get(f"/runs/{run_id}/result")
            assert result.status_code == 200
            assert result.json()["run_id"] == run_id
            assert result.json()["validated_candidates"]
    finally:
        service.close()


def test_operator_reviewed_profile_cannot_be_downgraded(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from spider_qwen.api.server import create_app

    monkeypatch.setenv("SPIDER_QWEN_ALLOW_LIVE", "1")
    monkeypatch.setenv("SPIDER_QWEN_DEFAULT_PROFILE", "reviewed_procurement")
    monkeypatch.setenv("SPIDER_QWEN_API_TOKENS", '{"secret":"alice"}')
    tracker = _Tracker(delay=0.01)
    service = RunService(state_dir=tmp_path, controller_builder=tracker.builder, allow_live=True)
    headers = {"Authorization": "Bearer secret"}
    try:
        with TestClient(create_app(run_service=service)) as client:
            downgrade = client.post(
                "/runs", json={"query": "weaker", "profile": "live_research"}, headers=headers,
            )
            assert downgrade.status_code == 403
            assert "requires reviewed_procurement" in downgrade.json()["detail"]
            demo = client.post(
                "/runs", json={"query": "fixture", "profile": "offline_demo"}, headers=headers,
            )
            assert demo.status_code == 202
            _wait_for(service, demo.json()["run_id"], owner="alice")
            legacy_live = client.post(
                "/run", json={"query": "legacy live", "offline": False}, headers=headers,
            )
            assert legacy_live.status_code == 200
            assert legacy_live.json()["profile"] == "reviewed_procurement"
            assert tracker.built[-1]["require_review"] is True
    finally:
        service.close()


def test_deadline_cancels_worker_and_persists_timeout(tmp_path):
    service = RunService(
        state_dir=tmp_path, controller_builder=_Tracker().builder,
        max_deadline_seconds=2,
    )
    try:
        started = service.start(
            {"query": "block", "profile": "offline_demo", "deadline_seconds": 1},
            owner="local",
        )
        final = _wait_for(service, started["run_id"], timeout=2)
        assert final["status"] == "timed_out"
        assert "1s deadline" in final["error"]
    finally:
        service.close()


def test_deadline_withholds_result_after_blocking_synchronous_tail(tmp_path):
    class Controller:
        async def run(self, *_args, **_kwargs):
            time.sleep(1.1)
            return {"must_not_publish": True}

    service = RunService(
        state_dir=tmp_path, controller_builder=lambda **_config: Controller(),
        max_deadline_seconds=1,
    )
    try:
        started = service.start(
            {"query": "blocking tail", "profile": "offline_demo", "deadline_seconds": 1},
            owner="local",
        )
        final = _wait_for(service, started["run_id"], timeout=2)
        assert final["status"] == "timed_out"
        with pytest.raises(RunNotReady, match="result exists only when completed"):
            service.result(started["run_id"], owner="local")
    finally:
        service.close()


def test_cancel_during_blocking_synchronous_tail_withholds_result(tmp_path):
    entered = threading.Event()

    class Controller:
        async def run(self, *_args, **_kwargs):
            entered.set()
            time.sleep(0.2)
            return {"must_not_publish": True}

    service = RunService(state_dir=tmp_path, controller_builder=lambda **_config: Controller())
    try:
        started = service.start({"query": "blocking tail", "profile": "offline_demo"}, owner="local")
        assert entered.wait(timeout=1)
        service.cancel(started["run_id"], owner="local")
        assert _wait_for(service, started["run_id"])["status"] == "cancelled"
        assert service.events(started["run_id"], owner="local")[-1]["kind"] == "cancellation_acknowledged"
    finally:
        service.close()
