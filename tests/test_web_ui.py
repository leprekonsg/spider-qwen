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
import asyncio
import os
import socket
import threading
import time

import pytest

pw_sync = pytest.importorskip("playwright.sync_api")
pytest.importorskip("fastapi")
uvicorn = pytest.importorskip("uvicorn")

HUNT_TIMEOUT_MS = 30_000  # real offline worker and lifecycle polling

_MODEL_CONTEXT_SHIM = """(() => {
  window.registeredTools = {};
  window.registrationAborts = 0;
  Object.defineProperty(document, 'modelContext', {value: {
    async registerTool(tool, {signal}) {
      window.registeredTools[tool.name] = tool;
      signal.addEventListener('abort', () => {
        if (window.registeredTools[tool.name] === tool) delete window.registeredTools[tool.name];
        window.registrationAborts += 1;
      });
    }
  }, configurable: true});
})();"""


class _ServerURL(str):
    service = None


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
        url = _ServerURL(f"http://127.0.0.1:{port}")
        url.service = app.state.run_service
        yield url
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
    _expect(page.get_by_text("score · /100").first)
    _expect(page.get_by_text("Discovered", exact=True).first)
    assert page.get_by_text("Review-ready", exact=True).count() == 0
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


def test_webmcp_reads_completed_run_and_unregisters_on_reset(page, server_url):
    # Emulate only the browser registration surface; every tool invocation
    # still calls the real HTTP application and persisted offline result.
    page.add_init_script(_MODEL_CONTEXT_SHIM)
    _run_hunt(page, server_url)
    page.wait_for_function("Object.keys(window.registeredTools).length === 5")
    actual = page.evaluate("""async () => {
      const tools = window.registeredTools;
      const run = await tools.get_current_run.execute({});
      const listed = await tools.list_candidates.execute({});
      const supplier = listed.candidates[0];
      const selector = {supplier_id: supplier.supplier_id};
      if (supplier.offering_id) selector.offering_id = supplier.offering_id;
      const evidence = await tools.get_candidate_evidence.execute(selector);
      const draft = await tools.get_rfq_draft.execute(selector);
      const stored = await (await fetch(`/runs/${run.run_id}/result`)).json();
      return {run, listed, evidence, draft, stored,
        readOnly: Object.values(tools).every(t => t.annotations.readOnlyHint),
        names: Object.keys(tools)};
    }""")
    assert actual["listed"]["candidates"] == actual["stored"]["validated_candidates"]
    assert actual["evidence"]["evidence_refs"] == actual["listed"]["candidates"][0]["evidence_refs"]
    assert actual["draft"]["submission_status"] == "unsent"
    assert actual["readOnly"] is True
    assert actual["listed"]["candidates"][0]["readiness"]["stage"] == "discovered"
    assert actual["evidence"]["readiness"] == actual["listed"]["candidates"][0]["readiness"]
    assert set(actual["names"]) == {"get_current_run", "list_candidates", "get_candidate_evidence", "compare_candidates", "get_rfq_draft"}
    page.evaluate("window.SQWebMCP.clear()")
    assert page.evaluate("Object.keys(window.registeredTools).length") == 0
    assert page.errors == []


def test_webmcp_registration_failure_keeps_ui_functional(page, server_url):
    page.add_init_script("""Object.defineProperty(document, 'modelContext', {
      value: {registerTool: async () => {throw new Error('Unsupported API revision');}}, configurable: true
    });""")
    _run_hunt(page, server_url)
    _expect(page.get_by_role("heading", level=3, name="Example Vendor 1 Pte Ltd").first)
    assert page.errors == []


def test_buyer_requirements_round_trip_and_remain_unresolved_in_dossier(page, server_url):
    page.goto(server_url)
    page.locator("summary").filter(has_text="Supplier requirements").click()
    page.get_by_role("textbox", name="Supplier requirements", exact=True).fill("overnight work\nsafety documentation")
    page.get_by_role("checkbox", name="This checklist includes all supplier conditions in my request").check()
    with page.expect_response(lambda r: r.url.endswith("/runs") and r.request.method == "POST") as started:
        page.get_by_role("button", name="Begin hunt").click()
    request = started.value.json()["procurement_request"]
    assert request["requirements_confirmed"] is True
    assert [r["text"] for r in request["requirements"]] == ["overnight work", "safety documentation"]
    _expect(page.get_by_text("stop ·"), timeout=HUNT_TIMEOUT_MS)
    page.get_by_role("button", name="Evidence", exact=True).first.click()
    _expect(page.get_by_text("Supplier qualification: unresolved"))
    _expect(page.get_by_text("overnight work", exact=True))
    _expect(page.get_by_text("safety documentation", exact=True))
    assert page.get_by_text("Review-ready", exact=True).count() == 0
    assert page.errors == []


def test_webmcp_switches_between_two_completed_runs(page, server_url, run_payload):
    page.add_init_script(_MODEL_CONTEXT_SHIM)
    page.goto(server_url)
    actual = page.evaluate("""async (firstRunId) => {
      const second = await window.SQAPI.run('A4 paper public price Singapore');
      await window.SQWebMCP.setCurrentRun(firstRunId);
      const first = await window.registeredTools.get_current_run.execute({});
      const abortsBeforeSwitch = window.registrationAborts;
      await window.SQWebMCP.setCurrentRun(second.run_id);
      const current = await window.registeredTools.get_current_run.execute({});
      return {
        firstId: first.run_id,
        secondId: current.run_id,
        expectedSecondId: second.run_id,
        abortsOnSwitch: window.registrationAborts - abortsBeforeSwitch,
        toolCount: Object.keys(window.registeredTools).length,
      };
    }""", run_payload["run_id"])

    assert actual["firstId"] == run_payload["run_id"]
    assert actual["secondId"] == actual["expectedSecondId"]
    assert actual["secondId"] != actual["firstId"]
    assert actual["abortsOnSwitch"] == 5
    assert actual["toolCount"] == 5
    assert page.errors == []


def test_webmcp_propagates_inflight_abort_signal(page, server_url, run_payload):
    page.add_init_script(_MODEL_CONTEXT_SHIM)
    page.goto(server_url)
    actual = page.evaluate("""async (runId) => {
      await window.SQWebMCP.setCurrentRun(runId);
      const nativeFetch = window.fetch;
      let sawSignal = false;
      window.fetch = (input, options = {}) => {
        if (String(input).includes('/inspect/get_current_run')) {
          sawSignal = options.signal instanceof AbortSignal;
          return new Promise((resolve, reject) => {
            options.signal.addEventListener('abort', () => {
              reject(new DOMException('inspect cancelled', 'AbortError'));
            }, {once: true});
          });
        }
        return nativeFetch(input, options);
      };
      const request = new AbortController();
      const pending = window.registeredTools.get_current_run.execute({}, {signal: request.signal});
      request.abort();
      try {
        await pending;
        return {sawSignal, errorName: null};
      } catch (error) {
        return {sawSignal, errorName: error.name};
      } finally {
        window.fetch = nativeFetch;
      }
    }""", run_payload["run_id"])

    assert actual == {"sawSignal": True, "errorName": "AbortError"}
    assert page.errors == []


def test_webmcp_pagehide_unregisters_and_pageshow_restores(page, server_url, run_payload):
    page.add_init_script(_MODEL_CONTEXT_SHIM)
    page.goto(server_url)
    page.evaluate("runId => window.SQWebMCP.setCurrentRun(runId)", run_payload["run_id"])
    page.wait_for_function("Object.keys(window.registeredTools).length === 5")

    page.evaluate("window.dispatchEvent(new Event('pagehide'))")
    assert page.evaluate("Object.keys(window.registeredTools).length") == 0
    assert page.evaluate("window.registrationAborts") == 5

    page.evaluate("window.dispatchEvent(new Event('pageshow'))")
    page.wait_for_function("Object.keys(window.registeredTools).length === 5")
    assert page.errors == []


def test_ui_maps_same_supplier_offerings_to_distinct_trust_and_drafts(page, server_url):
    page.goto(server_url)
    actual = page.evaluate("""() => {
      const result = {
        validated_candidates: [
          {supplier_id: 'supplier-a', offering_id: 'paper', vendor_name: 'Office Co',
           product_url: '/paper', price: 10, currency: 'SGD', pricing_status: 'EXACT_PRICE',
           evidence_refs: [], conflicting_fields: []},
          {supplier_id: 'supplier-a', offering_id: 'toner', vendor_name: 'Office Co',
           product_url: '/toner', price: 80, currency: 'SGD', pricing_status: 'EXACT_PRICE',
           evidence_refs: [], conflicting_fields: []},
        ],
        trust_verdicts: [
          {supplier_id: 'supplier-a', offering_id: 'paper', summary: 'paper trust'},
          {supplier_id: 'supplier-a', offering_id: 'toner', summary: 'toner trust'},
        ],
        rfq_drafts: [
          {vendor: {supplier_id: 'supplier-a', offering_id: 'paper'}, status: 'paper draft'},
          {vendor: {supplier_id: 'supplier-a', offering_id: 'toner'}, status: 'toner draft'},
        ],
        metrics: {},
      };
      const mapped = window.SQMAP.mapResult(result);
      return {
        ids: mapped.vendors.map(v => v.id),
        trust: mapped.vendors.map(v => v.trust && v.trust.summary),
        drafts: mapped.vendors.map(v => mapped.rfqByVendor[v.id] && mapped.rfqByVendor[v.id].status),
      };
    }""")

    assert len(set(actual["ids"])) == 2
    assert actual["trust"] == ["paper trust", "toner trust"]
    assert actual["drafts"] == ["paper draft", "toner draft"]
    assert page.errors == []


def test_halt_waits_for_real_worker_cancellation(page, server_url, monkeypatch):
    import httpx

    builder = server_url.service.controller_builder
    cancelled = threading.Event()

    def delayed_builder(**kwargs):
        controller = builder(**kwargs)
        class DelayedController:
            async def run(self, *args, **options):
                try:
                    await asyncio.sleep(30)
                    return await controller.run(*args, **options)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
        return DelayedController()

    monkeypatch.setattr(server_url.service, "controller_builder", delayed_builder)
    page.goto(server_url)
    with page.expect_response(lambda response: response.url.endswith("/runs") and response.request.method == "POST") as started:
        page.get_by_role("button", name="Begin hunt").click()
    run_id = started.value.json()["run_id"]
    page.get_by_role("button", name="Halt run").click()
    _expect(page.get_by_role("button", name="Begin hunt"))
    response = httpx.get(f"{server_url}/runs/{run_id}")
    assert response.json()["status"] == "cancelled"
    assert cancelled.wait(1)
    events = httpx.get(f"{server_url}/runs/{run_id}/events").json()["events"]
    assert any(e["kind"] == "cancellation_acknowledged" for e in events)
    assert not any(e["kind"] == "completed" for e in events)
    assert page.get_by_text("stop ·").count() == 0
    assert page.errors == []


def test_running_ui_displays_persisted_worker_events(page, server_url, monkeypatch):
    import httpx

    builder = server_url.service.controller_builder
    release = threading.Event()

    def held_builder(**kwargs):
        controller = builder(**kwargs)

        class HeldController:
            async def run(self, *args, **options):
                result = await controller.run(*args, **options)
                while not release.is_set():
                    await asyncio.sleep(0.05)
                return result

        return HeldController()

    monkeypatch.setattr(server_url.service, "controller_builder", held_builder)
    try:
        page.goto(server_url)
        with page.expect_response(lambda r: r.url.endswith("/runs") and r.request.method == "POST") as started:
            page.get_by_role("button", name="Begin hunt").click()
        run_id = started.value.json()["run_id"]
        _expect(page.get_by_text("consolidation: supplier consolidation (success)").first,
                timeout=HUNT_TIMEOUT_MS)
        _expect(page.get_by_text(f"· {run_id}", exact=True))
        assert httpx.get(f"{server_url}/runs/{run_id}").json()["status"] == "running"
        events = httpx.get(f"{server_url}/runs/{run_id}/events").json()["events"]
        traces = [e for e in events if e["kind"] == "trace"]
        assert {"discovery", "retrieval", "consolidation"} <= {e["phase"] for e in traces}
        for event in traces:
            assert page.get_by_text(event["message"], exact=True).count() >= 1
        assert page.get_by_text("stop ·").count() == 0
        assert page.errors == []
    finally:
        release.set()
    _expect(page.get_by_text("stop ·"), timeout=HUNT_TIMEOUT_MS)


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
