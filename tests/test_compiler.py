"""T-1.4: LLM-Compiler parallel tool DAG + token-bucket rate limits.

Concurrency and rate-limit timing use injected fake clocks where needed to stay
deterministic (no real minute-long waits).
"""

from __future__ import annotations

import asyncio
import time

from spider_qwen.agent.compiler import (
    LLMCompiler,
    NullRateLimiter,
    RateLimiter,
    ToolNode,
    TokenBucket,
)


async def _const(x):
    return x


def test_token_bucket_allows_burst_to_capacity():
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    async def go():
        b = TokenBucket(5, now=lambda: 0.0, sleep=fake_sleep)  # 5/min, capacity 5
        for _ in range(5):
            await b.acquire()
        assert sleeps == []  # full burst, no throttle

    asyncio.run(go())


def test_token_bucket_throttles_beyond_capacity():
    clock = {"t": 0.0}
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)
        clock["t"] += s

    async def go():
        b = TokenBucket(5, now=lambda: clock["t"], sleep=fake_sleep)  # 1 token / 12s
        for _ in range(5):
            await b.acquire()
        assert sleeps == []
        await b.acquire()  # 6th: must wait ~12s for one token
        assert sleeps and abs(sleeps[-1] - 12.0) < 1e-6

    asyncio.run(go())


def test_compiler_executes_dag_with_dependencies():
    order: list[str] = []

    def mk(node_id):
        async def run(dep):
            order.append(node_id)
            return node_id
        return run

    nodes = [
        ToolNode(id="search", kind="search", run=mk("search")),
        ToolNode(id="fetch", kind="fetch", run=mk("fetch"), deps=("search",)),
    ]
    results, trace = asyncio.run(LLMCompiler().execute(nodes))
    assert results["search"] == "search" and results["fetch"] == "fetch"
    assert order == ["search", "fetch"]  # dependency respected
    assert trace.edges == [{"from": "search", "to": "fetch"}]
    assert trace.levels == [["search"], ["fetch"]]


def test_compiler_runs_fetches_concurrently_under_sequential():
    latency = 0.05
    vendors = [f"https://v{i}.example" for i in range(5)]
    regions = ["SG", "MY", "global"]

    async def slow(value):
        await asyncio.sleep(latency)
        return value

    nodes = [ToolNode(id=f"s{i}", kind="search", run=(lambda dep, q=q: slow(q)))
             for i, q in enumerate(regions)]
    nodes += [ToolNode(id=f"f{i}", kind="fetch", run=(lambda dep, u=u: slow(u)))
              for i, u in enumerate(vendors)]

    compiler = LLMCompiler(rate_limiter=RateLimiter(search_per_minute=600, fetch_per_minute=600))
    t0 = time.monotonic()
    results, trace = asyncio.run(compiler.execute(nodes))
    elapsed = time.monotonic() - t0

    sequential = latency * len(nodes)  # 8 * 0.05 = 0.4s
    assert elapsed < sequential * 0.6, f"{elapsed} not < {sequential * 0.6}"
    assert trace.peak_by_kind.get("fetch", 0) >= 5  # >=5 concurrent fetches
    assert len(results) == len(nodes)


def test_compiler_enforces_fetch_rate_limit():
    clock = {"t": 0.0}

    async def fake_sleep(s):
        clock["t"] += s

    nodes = [ToolNode(id=f"f{i}", kind="fetch", run=(lambda dep: _const(1))) for i in range(30)]
    rl = RateLimiter(search_per_minute=5, fetch_per_minute=25,
                     now=lambda: clock["t"], sleep=fake_sleep)
    results, trace = asyncio.run(LLMCompiler(rate_limiter=rl).execute(nodes))
    assert len(results) == 30
    assert clock["t"] > 0  # 25 burst + 5 throttled -> clock advanced


def test_compiler_records_dag_trace_to_tracer():
    from spider_qwen.observability.tracing import Tracer

    nodes = [
        ToolNode(id="a", kind="search", run=(lambda dep: _const(1))),
        ToolNode(id="b", kind="fetch", run=(lambda dep: _const(2)), deps=("a",)),
    ]
    tracer = Tracer("run_x", "service_quote_required")
    asyncio.run(LLMCompiler().execute(nodes, tracer=tracer))
    events = [e for e in tracer.events if e.step == "compiler_execute"]
    assert events and events[0].detail["edges"] == [{"from": "a", "to": "b"}]
    assert events[0].detail["max_concurrency"] >= 1


def test_compiler_rejects_unknown_dependency():
    nodes = [ToolNode(id="a", kind="fetch", run=(lambda dep: _const(1)), deps=("ghost",))]
    try:
        asyncio.run(LLMCompiler().execute(nodes))
    except ValueError as exc:
        assert "ghost" in str(exc)
    else:
        raise AssertionError("expected ValueError for unknown dependency")


def test_controller_primary_gather_records_query_expansion():
    import asyncio

    from spider_qwen.agent.controller import Controller
    from spider_qwen.tools.fetch_service import MockFetchProvider
    from spider_qwen.tools.search_service import MockSearchProvider, SearchService

    recorded: list[str] = []

    class RecordingSearch(MockSearchProvider):
        async def search(self, query, location, language, limit):
            recorded.append(query)
            return await super().search(query, location, language, limit)

    controller = Controller(search_provider=RecordingSearch(),
                            fetch_provider=MockFetchProvider(), state_dir=None, persist=False)
    asyncio.run(controller.run(
        "16-pin SMT op-amp TI obsolete replacement Singapore",
        mode="product_exact_price",
    ))
    assert recorded
    assert any("nrnd" in q.lower() or "obsolete" in q.lower() for q in recorded)
    assert len(set(recorded)) >= 2


def test_controller_fetch_runs_concurrently_in_gather():
    import asyncio
    import time

    from spider_qwen.agent.budget import BudgetTracker
    from spider_qwen.agent.controller import Controller
    from spider_qwen.agent.execution_context import ExecutionContext, new_run_id
    from spider_qwen.agent.policy import load_policy
    from spider_qwen.evidence.ledger import EvidenceLedger
    from spider_qwen.memory.working import WorkingMemory
    from spider_qwen.modes.contracts import ProcurementMode
    from spider_qwen.modes.router import ModeRouter
    from spider_qwen.observability.tracing import Tracer
    from spider_qwen.tools.fetch_service import FetchService, MockFetchProvider
    from spider_qwen.tools.search_service import MockSearchProvider, SearchService
    latency = 0.05
    state = {"in_flight": 0, "peak": 0}

    class SlowFetch(MockFetchProvider):
        async def fetch(self, urls, output_format="markdown", include_links=True):
            state["in_flight"] += 1
            state["peak"] = max(state["peak"], state["in_flight"])
            await asyncio.sleep(latency)
            try:
                return await super().fetch(urls, output_format, include_links)
            finally:
                state["in_flight"] -= 1

    controller = Controller(
        search_provider=MockSearchProvider(),
        fetch_provider=SlowFetch(),
        state_dir=None,
        persist=False,
    )
    controller.compiler = LLMCompiler(RateLimiter(search_per_minute=600, fetch_per_minute=600))
    mode = ProcurementMode.SERVICE_QUOTE_REQUIRED
    route = ModeRouter().route(mode)
    budget = load_policy().budget_for(mode, route.budget_key)
    run_id = new_run_id()
    ledger = EvidenceLedger(run_id, None)
    tracker = BudgetTracker(budget)
    working = WorkingMemory(run_id=run_id, query="office cleaning Singapore", mode=mode.value)
    tracer = Tracer(run_id, mode.value)
    ctx = ExecutionContext(run_id=run_id, query="office cleaning Singapore", mode=mode,
                           ledger=ledger, tracker=tracker, working=working, tracer=tracer)
    search = SearchService(MockSearchProvider(), ledger, tracker, tracer)
    fetch = FetchService(SlowFetch(), ledger, tracker, tracer)

    queries = [f"office cleaning Singapore vendor {i}" for i in range(5)]
    t0 = time.monotonic()
    cands = asyncio.run(controller.gather_parallel(
        ctx, route, queries, search, fetch, location="SG", target_country="Singapore"))
    elapsed = time.monotonic() - t0

    assert cands
    assert state["peak"] >= 5, f"peak concurrent fetches {state['peak']}"
    sequential = latency * 5
    assert elapsed < sequential * 0.75, f"{elapsed:.3f}s not faster than sequential {sequential:.3f}s"
    fetch_events = [e for e in tracer.events if e.step == "compiler_execute"]
    assert fetch_events
    assert fetch_events[-1].detail.get("peak_by_kind", {}).get("fetch", 0) >= 5


def test_controller_gather_parallel_uses_compiler():
    from spider_qwen.agent.budget import BudgetTracker
    from spider_qwen.agent.controller import Controller
    from spider_qwen.agent.execution_context import ExecutionContext, new_run_id
    from spider_qwen.agent.policy import load_policy
    from spider_qwen.evidence.ledger import EvidenceLedger
    from spider_qwen.memory.working import WorkingMemory
    from spider_qwen.modes.contracts import ProcurementMode
    from spider_qwen.modes.router import ModeRouter
    from spider_qwen.observability.tracing import Tracer
    from spider_qwen.tools.fetch_service import FetchService, MockFetchProvider
    from spider_qwen.tools.search_service import MockSearchProvider, SearchService

    controller = Controller(search_provider=MockSearchProvider(),
                            fetch_provider=MockFetchProvider(), state_dir=None, persist=False)
    mode = ProcurementMode.SERVICE_QUOTE_REQUIRED
    route = ModeRouter().route(mode)
    budget = load_policy().budget_for(mode, route.budget_key)
    run_id = new_run_id()
    ledger = EvidenceLedger(run_id, None)
    tracker = BudgetTracker(budget)
    working = WorkingMemory(run_id=run_id, query="office cleaning Singapore", mode=mode.value)
    tracer = Tracer(run_id, mode.value)
    ctx = ExecutionContext(run_id=run_id, query="office cleaning Singapore", mode=mode,
                           ledger=ledger, tracker=tracker, working=working, tracer=tracer)
    search = SearchService(MockSearchProvider(), ledger, tracker, tracer)
    fetch = FetchService(MockFetchProvider(), ledger, tracker, tracer)

    cands = asyncio.run(controller.gather_parallel(
        ctx, route,
        ["office cleaning Singapore quotation", "office cleaning Singapore vendor"],
        search, fetch, location="SG", target_country="Singapore"))

    assert cands
    assert any(e.step == "compiler_execute" for e in tracer.events)


def test_offline_controller_does_not_throttle_mock_providers():
    """Regression: mock providers must bypass the wall-clock rate limiter.

    Routing concurrent search/fetch through the rate-limited compiler once made
    the 80-case offline benchmark block ~1h on token refills (~45s per case
    after the 5-token burst drained). Mock providers consume no external quota,
    so they must use NullRateLimiter and never wait.
    """
    from spider_qwen.agent.controller import Controller
    from spider_qwen.tools.fetch_service import MockFetchProvider
    from spider_qwen.tools.search_service import MockSearchProvider

    controller = Controller(search_provider=MockSearchProvider(),
                            fetch_provider=MockFetchProvider(), state_dir=None, persist=False)
    assert isinstance(controller.rate_limiter, NullRateLimiter)

    # Each run fires several searches; if throttled, the 2nd+ run alone would
    # block tens of seconds. Three runs well under 5s proves no wall-clock wait.
    t0 = time.monotonic()
    for _ in range(3):
        asyncio.run(controller.run("office cleaning services Singapore", mode="auto"))
    assert time.monotonic() - t0 < 5.0


def test_controller_throttles_when_a_provider_hits_a_live_quota():
    """A live-quota provider must still get a real RateLimiter (protect the API)."""
    from spider_qwen.agent.controller import Controller
    from spider_qwen.tools.search_service import MockSearchProvider

    class LiveFetchStub:
        rate_limited = True
        provider_name = "stub"
        fetch_source_tool = "stub"

        async def fetch(self, urls, output_format="markdown", include_links=True):
            raise AssertionError("provider should not be called in this wiring test")

    controller = Controller(search_provider=MockSearchProvider(),
                            fetch_provider=LiveFetchStub(), state_dir=None, persist=False)
    assert isinstance(controller.rate_limiter, RateLimiter)
