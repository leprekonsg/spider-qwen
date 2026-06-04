"""Trust-layer data flow, end to end: no silent failure.

These tests exercise the seams the unit tests cannot: one LONG-LIVED
controller serving consecutive runs against one state dir. They prove:

- run 2 recalls what run 1 promoted (shared SemanticMemory instance;
  a second instance over the same semantic.json used to clobber facts),
- the full citation loop closes only under verification: promote ->
  recall -> attach -> SAFE re-ground against the CURRENT corpus ->
  credit -> persist,
- the GSAR decision and GRADE surface in RunResult.metrics instead of
  dead-ending inside the verifier,
- when crediting is impossible (verification off) the audit trail says
  so explicitly.
"""

from __future__ import annotations

import asyncio
import json

from spider_qwen.agent.controller import Controller
from spider_qwen.tools.fetch_service import MockFetchProvider
from spider_qwen.tools.provider_types import SearchResult, SearchResultSet
from spider_qwen.tools.search_service import MockSearchProvider

VENDOR_PAGE_RUN1 = "https://example-cleaning.sg/services"
VENDOR_PAGE_RUN2 = "https://example-cleaning.sg/about"
DIRECTORY_PAGE = "https://sg-services-directory.example/cleaning"

FIXTURES = {
    # Run 1: the vendor page carries the quote channel; extraction finds it,
    # the candidate validates, and the fact is promoted to semantic memory.
    VENDOR_PAGE_RUN1: {
        "title": "Example Cleaning Pte Ltd",
        "text": (
            "Example Cleaning Pte Ltd provides office cleaning services in "
            "Singapore and accepts quotation requests at sales@example-cleaning.sg. "
            "Daily, weekly and one-off office cleaning packages."
        ),
    },
    # Run 2: the vendor page has NO quote channel -- only memory recall can
    # fill it -- while the directory page independently co-locates vendor and
    # email so SAFE corpus re-grounding can verify the recalled claim.
    VENDOR_PAGE_RUN2: {
        "title": "Example Cleaning Pte Ltd",
        "text": (
            "Example Cleaning Pte Ltd provides office cleaning services in "
            "Singapore. We serve offices island-wide with trained crews."
        ),
    },
    DIRECTORY_PAGE: {
        "title": "SG Services Directory",
        "text": (
            "Vendor directory for Singapore. Example Cleaning Pte Ltd offers "
            "office cleaning, contact sales@example-cleaning.sg for quotations."
        ),
    },
}


class _PhasedSearch:
    """Returns run-1 URLs in phase 1 and run-2 URLs in phase 2, regardless of
    how the planner rewrites the query (immune to query-text matching)."""

    provider_name = "mock"
    search_source_tool = "mock"

    def __init__(self) -> None:
        self.phase = 1

    async def search(self, query: str, location: str | None, language: str, limit: int):
        urls = [VENDOR_PAGE_RUN1] if self.phase == 1 else [VENDOR_PAGE_RUN2, DIRECTORY_PAGE]
        results = [
            SearchResult(url=u, title=FIXTURES[u]["title"],
                         snippet="office cleaning Singapore quotation",
                         rank=i, source_tool="mock")
            for i, u in enumerate(urls)
        ]
        return SearchResultSet(query=query, location=location, results=results,
                               total_results=len(results), provider="mock")


def _controller(tmp_path, *, verify: bool, search=None) -> Controller:
    return Controller(
        search_provider=search or _PhasedSearch(),
        fetch_provider=MockFetchProvider(fixtures=FIXTURES),
        verify=verify,
        state_dir=tmp_path,
        persist=True,
    )


def _semantic_facts(tmp_path) -> list[dict]:
    path = tmp_path / "memory" / "semantic.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def _audit_actions(tmp_path, run_id: str) -> list[str]:
    payload = json.loads(
        (tmp_path / "audit" / f"{run_id}.audit.json").read_text(encoding="utf-8")
    )
    events = payload["events"] if isinstance(payload, dict) and "events" in payload else payload
    return [e["action"] for e in events]


QUERY = "Example Cleaning office cleaning Singapore quotation"


def test_citation_loop_closes_end_to_end_under_verification(tmp_path):
    controller = _controller(tmp_path, verify=True)

    first = asyncio.run(controller.run(QUERY, mode="service_quote_required"))
    assert first.validated_candidates, "run 1 must validate the vendor"
    facts_after_1 = _semantic_facts(tmp_path)
    quote_facts = [f for f in facts_after_1 if f["field"] == "quote_channel"]
    assert quote_facts, "run 1 must promote the quote_channel fact"
    assert all(f["citation_count"] == 0 for f in quote_facts)
    ids_after_1 = {f["fact_id"] for f in facts_after_1}

    controller.search_provider.phase = 2
    second = asyncio.run(controller.run(QUERY, mode="service_quote_required"))

    # The long-lived controller recalls what run 1 promoted (one shared
    # SemanticMemory instance -- a stale second instance would recall nothing
    # and, worse, clobber run 1's facts when it persisted).
    assert second.metrics["memory_recalls"] >= 1
    cands = second.validated_candidates
    assert any(
        (c.get("quote_channel") or {}).get("value") == "sales@example-cleaning.sg"
        for c in cands
    ), "run 2's vendor page has no quote channel; only memory recall can fill it"

    # The recalled claim was re-grounded against the CURRENT corpus (the
    # directory page) by the spine, so the fact earns a citation -- persisted.
    facts_after_2 = _semantic_facts(tmp_path)
    assert ids_after_1 <= {f["fact_id"] for f in facts_after_2}, \
        "run 2 must never drop run 1's persisted facts"
    credited = [f for f in facts_after_2 if f["field"] == "quote_channel"
                and f["citation_count"] >= 1]
    assert credited, "verified reuse must increment citation_count on disk"
    assert "memory_citations_recorded" in _audit_actions(tmp_path, second.run_id)

    # GSAR decision + GRADE surface end to end, not just inside the verifier.
    assessments = second.metrics["verification_assessments"]
    assert assessments, "kept candidates must surface decision and grade"
    for entry in assessments.values():
        assert entry["decision"] in {"proceed", "regenerate", "replan"}
        assert entry["grade"] in {"high", "moderate", "low", "very_low"}
    assert second.metrics["claims_verified"] >= 1


def test_citations_are_skipped_loudly_when_verification_is_off(tmp_path):
    controller = _controller(tmp_path, verify=False)

    asyncio.run(controller.run(QUERY, mode="service_quote_required"))
    controller.search_provider.phase = 2
    second = asyncio.run(controller.run(QUERY, mode="service_quote_required"))

    assert second.metrics["memory_recalls"] >= 1
    # Without the spine there is no external check breaking the recall ->
    # validate -> credit loop, so crediting must not happen -- and the audit
    # trail must say why, not stay silent.
    assert all(f["citation_count"] == 0 for f in _semantic_facts(tmp_path))
    actions = _audit_actions(tmp_path, second.run_id)
    assert "memory_citations_skipped" in actions
    assert "memory_citations_recorded" not in actions
    # Stable metrics shape either way.
    assert second.metrics["verification_assessments"] == {}


def test_long_lived_controller_runs_share_one_semantic_memory(tmp_path):
    controller = _controller(tmp_path, verify=False, search=MockSearchProvider())
    assert controller._semantic_memory() is controller.memory_mcp.memory

    first = asyncio.run(controller.run("office cleaning Singapore", mode="auto"))
    assert first.validated_candidates
    ids_after_1 = {f["fact_id"] for f in _semantic_facts(tmp_path)}
    assert ids_after_1

    second = asyncio.run(controller.run("office cleaning Singapore", mode="auto"))
    # Same query, same controller: the second run must see the first run's
    # facts (stale-instance recall returned nothing here before the fix)...
    assert second.metrics["memory_recalls"] >= 1
    # ...and persisting run 2 must keep every fact run 1 wrote.
    assert ids_after_1 <= {f["fact_id"] for f in _semantic_facts(tmp_path)}
