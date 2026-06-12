"""Browser smoke tests for the static frontend (web/) against the real
FastAPI server running offline mock providers.

What "fit for purpose" means here: the UI is a demo workspace whose core
promise is honesty -- every rendered value traces to a real backend field.
These tests pin that promise end to end: the page boots with zero external
requests (vendored React), a hunt round-trips the real /run endpoint, and the
honesty surfaces (seam chip, trust verdict, proof-backed ledger statuses)
render from genuine RunResult data.

Setup (skipped automatically when anything is missing):
    pip install -e ".[server,ui]"
    python -m playwright install chromium
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time

import pytest

pw_sync = pytest.importorskip("playwright.sync_api")
pytest.importorskip("fastapi")
uvicorn = pytest.importorskip("uvicorn")

HUNT_TIMEOUT_MS = 30_000  # offline run + ~3s UI theatre window


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server_url(tmp_path_factory):
    """The real FastAPI app (offline mocks) on an ephemeral port, temp state dir."""
    old_state = os.environ.get("SPIDER_QWEN_STATE_DIR")
    os.environ["SPIDER_QWEN_STATE_DIR"] = str(tmp_path_factory.mktemp("state"))
    try:
        from spider_qwen.api.server import create_app

        app = create_app()
        port = _free_port()
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.time() + 15
        while not server.started:
            if time.time() > deadline:
                raise RuntimeError("uvicorn did not start within 15s")
            time.sleep(0.05)
        yield f"http://127.0.0.1:{port}"
        server.should_exit = True
        thread.join(timeout=5)
    finally:
        if old_state is None:
            os.environ.pop("SPIDER_QWEN_STATE_DIR", None)
        else:
            os.environ["SPIDER_QWEN_STATE_DIR"] = old_state


@pytest.fixture(scope="module")
def browser():
    with pw_sync.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # browser binary not downloaded
            pytest.skip(f"chromium unavailable ({exc}); run: python -m playwright install chromium")
        yield b
        b.close()


@pytest.fixture()
def page(browser):
    """Fresh page per test, recording console/page errors and external requests."""
    pg = browser.new_page()
    pg.errors = []
    pg.external_requests = []
    pg.on("pageerror", lambda e: pg.errors.append(f"pageerror: {e}"))
    pg.on("console", lambda m: pg.errors.append(f"console: {m.text}") if m.type == "error" else None)
    pg.on(
        "request",
        lambda r: pg.external_requests.append(r.url) if "127.0.0.1" not in r.url else None,
    )
    # Broken local assets (404 fonts, images, scripts) must fail the boot test.
    pg.on(
        "response",
        lambda r: pg.errors.append(f"http {r.status}: {r.url}") if r.status >= 400 else None,
    )
    yield pg
    pg.close()


def _expect(locator, timeout=5_000):
    pw_sync.expect(locator).to_be_visible(timeout=timeout)


def _run_hunt(page, server_url):
    page.goto(server_url)
    page.get_by_role("button", name="Begin hunt").click()
    # The results header chip appears once the run commits.
    _expect(page.get_by_text("stop ·"), timeout=HUNT_TIMEOUT_MS)


def test_idle_page_boots_offline_with_no_errors(page, server_url):
    page.goto(server_url)
    _expect(page.get_by_role("heading", level=1))
    # Sidebar is honest before any run: no invented provider/ledger values.
    _expect(page.get_by_text("no run yet"))
    assert page.errors == []
    # Vendored React/Babel: the demo must not depend on any external host.
    assert page.external_requests == []


def test_hunt_renders_results_from_real_run(page, server_url):
    _run_hunt(page, server_url)
    # Seam honesty chip: offline mocks must never claim live Qwen calls.
    _expect(page.get_by_text("offline · deterministic").first)
    # Shortlist rendered real validated candidates from /run.
    _expect(page.get_by_role("heading", level=3, name="Example Vendor 1 Pte Ltd").first)
    _expect(page.get_by_text("score · /95").first)
    assert page.errors == []


def test_vendor_dossier_shows_trust_verdict_and_proof_backed_ledger(page, server_url):
    _run_hunt(page, server_url)
    page.get_by_role("button", name="Evidence", exact=True).first.click()
    _expect(page.get_by_text("Vendor dossier"))
    _expect(page.get_by_text("Trust verdict"))
    # Offline runs disable claim verification; the UI must say so, not fake it.
    _expect(page.get_by_text("claim verification disabled").first)
    # Ledger status comes from citation_proofs: "proven" (proof shipped) or
    # "recorded" -- the old hardcoded "verified" must not reappear.
    dossier_ledger = page.get_by_text("append-only · sha256").locator("xpath=ancestor::section")
    statuses = dossier_ledger.get_by_text("proven").or_(dossier_ledger.get_by_text("recorded"))
    pw_sync.expect(statuses.first).to_be_visible(timeout=5_000)
    assert dossier_ledger.get_by_text("verified", exact=True).count() == 0
    assert page.errors == []


@pytest.fixture(scope="module")
def run_payload(server_url):
    """One real offline RunResult, replayed by the route-interception tests."""
    import httpx

    resp = httpx.post(
        f"{server_url}/run",
        json={"query": "pest control services for office building Singapore"},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()


def test_instant_result_streams_ledger_without_crash(page, server_url, run_payload):
    """A /run that resolves before the theatre window ends must not crash.

    Regression: the evidence ticker read ``led[shown]`` inside the React state
    updater, which runs after ``shown += 1`` -- the final tick pushed
    ``undefined`` and HuntInProgress threw on ``e.id``. Six ledger rows at
    240ms/row exhaust the ticker inside the ~2.9s theatre window, hitting the
    exact tick the old code crashed on.
    """
    payload = dict(run_payload)
    payload["evidence_refs"] = payload["evidence_refs"][:6]
    page.route("**/run", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(payload)))
    page.goto(server_url)
    page.get_by_role("button", name="Begin hunt").click()
    _expect(page.get_by_text("stop ·"), timeout=HUNT_TIMEOUT_MS)
    assert page.errors == []


def test_slow_result_still_commits_after_theatre_window(page, server_url, run_payload):
    """A run that lands long after the theatre window must still commit.

    Regression: finalize() stopped polling after a 12s ceiling, stranding the
    hunt screen forever on live runs (which take minutes). 16s clears the old
    ceiling plus the theatre window.
    """
    body = json.dumps(run_payload)

    def slow(route):
        time.sleep(16)
        route.fulfill(status=200, content_type="application/json", body=body)

    page.route("**/run", slow)
    page.goto(server_url)
    page.get_by_role("button", name="Begin hunt").click()
    _expect(page.get_by_text("stop ·"), timeout=40_000)
    assert page.errors == []


def test_insights_shows_reasoning_and_telemetry_cards(page, server_url):
    _run_hunt(page, server_url)
    page.locator("main").get_by_role("button", name="Insights").click()
    _expect(page.get_by_text("Discovery reasoning"))
    _expect(page.get_by_text("Run telemetry"))
    # Real reasoning fields, not placeholders.
    _expect(page.get_by_text("crag", exact=True))
    _expect(page.get_by_text("Mode classification trace"))
    _expect(page.get_by_text("electronics_substitution"))
    assert page.errors == []
