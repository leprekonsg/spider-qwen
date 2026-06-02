"""T-1.4: LLM-Compiler-style parallel tool DAG with token-bucket rate limits.

A planner (qwen3.7-max, in spirit) emits a DAG of tool calls; the executor runs
independent nodes concurrently via ``asyncio.gather`` while respecting the
TinyFish free-tier limits (5 search/min, 25 fetch/min) through per-kind token
buckets. The default sequential controller pipeline is unchanged; the controller
exposes ``gather_parallel`` (used from T-3.3 GRAM-lite) built on this executor.

Buckets accept injectable ``now``/``sleep`` so rate-limit behaviour is testable
without minute-long real waits.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

NowFn = Callable[[], float]
SleepFn = Callable[[float], Awaitable[None]]


class TokenBucket:
    """Async token bucket: burst up to ``capacity``, refill at ``rate_per_minute``."""

    def __init__(
        self,
        rate_per_minute: float,
        capacity: float | None = None,
        *,
        now: NowFn | None = None,
        sleep: SleepFn | None = None,
    ) -> None:
        self.rate_per_minute = float(rate_per_minute)
        self.rate = self.rate_per_minute / 60.0  # tokens/sec
        self.capacity = float(capacity if capacity is not None else max(1.0, rate_per_minute))
        self._tokens = self.capacity
        self._now = now or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._last = self._now()
        self._lock = asyncio.Lock()

    async def acquire(self, n: float = 1) -> None:
        async with self._lock:
            while True:
                now = self._now()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= n:
                    self._tokens -= n
                    return
                wait = (n - self._tokens) / self.rate if self.rate > 0 else 0.0
                await self._sleep(max(wait, 0.0))


class RateLimiter:
    """Per-tool-kind token buckets (search + fetch by default)."""

    def __init__(
        self,
        search_per_minute: float = 5,
        fetch_per_minute: float = 25,
        *,
        now: NowFn | None = None,
        sleep: SleepFn | None = None,
    ) -> None:
        self.buckets: dict[str, TokenBucket] = {
            "search": TokenBucket(search_per_minute, now=now, sleep=sleep),
            "fetch": TokenBucket(fetch_per_minute, now=now, sleep=sleep),
        }

    async def acquire(self, kind: str, n: float = 1) -> None:
        bucket = self.buckets.get(kind)
        if bucket is not None:
            await bucket.acquire(n)


@dataclass
class ToolNode:
    id: str
    kind: str  # search | fetch | extract | ocr | ...
    run: Callable[[dict[str, Any]], Awaitable[Any]]  # receives {dep_id: result}
    deps: tuple[str, ...] = ()
    cost: float = 1.0  # tokens consumed from the kind's bucket


@dataclass
class DagTrace:
    nodes: list[dict[str, str]] = field(default_factory=list)
    edges: list[dict[str, str]] = field(default_factory=list)
    levels: list[list[str]] = field(default_factory=list)
    max_concurrency: int = 0
    peak_by_kind: dict[str, int] = field(default_factory=dict)


class LLMCompiler:
    """Execute a tool-call DAG: independent nodes run concurrently per level."""

    def __init__(self, rate_limiter: RateLimiter | None = None, max_concurrency: int = 16) -> None:
        self.rate_limiter = rate_limiter or RateLimiter()
        self.max_concurrency = max_concurrency

    @staticmethod
    def _levels(nodes: list[ToolNode]) -> list[list[ToolNode]]:
        by_id = {n.id: n for n in nodes}
        for n in nodes:
            for d in n.deps:
                if d not in by_id:
                    raise ValueError(
                        f"Tool node '{n.id}' depends on unknown node '{d}'. "
                        f"Declare '{d}' in the DAG or fix the dependency."
                    )
        resolved: set[str] = set()
        levels: list[list[ToolNode]] = []
        remaining = list(nodes)
        while remaining:
            ready = [n for n in remaining if all(d in resolved for d in n.deps)]
            if not ready:
                raise ValueError("Cycle detected in tool DAG: " + ", ".join(n.id for n in remaining))
            levels.append(ready)
            resolved.update(n.id for n in ready)
            remaining = [n for n in remaining if n.id not in resolved]
        return levels

    async def execute(self, nodes: list[ToolNode], *, tracer: Any | None = None):
        levels = self._levels(nodes)
        results: dict[str, Any] = {}
        sem = asyncio.Semaphore(self.max_concurrency)
        state = {"current": 0, "peak": 0}
        by_kind: dict[str, int] = {}
        peak_by_kind: dict[str, int] = {}
        lock = asyncio.Lock()
        trace = DagTrace()

        async def run_node(node: ToolNode) -> None:
            await self.rate_limiter.acquire(node.kind, node.cost)
            async with sem:
                async with lock:
                    state["current"] += 1
                    state["peak"] = max(state["peak"], state["current"])
                    by_kind[node.kind] = by_kind.get(node.kind, 0) + 1
                    peak_by_kind[node.kind] = max(peak_by_kind.get(node.kind, 0), by_kind[node.kind])
                try:
                    dep_results = {d: results[d] for d in node.deps}
                    results[node.id] = await node.run(dep_results)
                finally:
                    async with lock:
                        state["current"] -= 1
                        by_kind[node.kind] -= 1

        for level in levels:
            trace.levels.append([n.id for n in level])
            await asyncio.gather(*(run_node(n) for n in level))

        trace.nodes = [{"id": n.id, "kind": n.kind} for n in nodes]
        trace.edges = [{"from": d, "to": n.id} for n in nodes for d in n.deps]
        trace.max_concurrency = state["peak"]
        trace.peak_by_kind = peak_by_kind
        if tracer is not None:
            tracer.record(
                step="compiler_execute", tool="llm_compiler", status="success",
                output_count=len(nodes),
                detail={"levels": trace.levels, "edges": trace.edges,
                        "max_concurrency": trace.max_concurrency,
                        "peak_by_kind": trace.peak_by_kind},
            )
        return results, trace
