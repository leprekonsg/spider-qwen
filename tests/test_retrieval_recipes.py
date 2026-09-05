from __future__ import annotations

import asyncio
from copy import deepcopy

import pytest
from pydantic import ValidationError

from spider_qwen.agent.budget import Budget, BudgetTracker
from spider_qwen.agent.controller import Controller
from spider_qwen.agent.policy import Policy, load_policy
from spider_qwen.application.retrieval_recipes import (
    PromotionPolicy,
    RecipeBudget,
    RecipeDriftGuard,
    RecipeEvidenceConditions,
    RecipeExecutionReport,
    RecipeJudgement,
    RecipePilot,
    RecipePreconditions,
    RecipeStateStore,
    RecipeStep,
    RetrievalRecipe,
)
from spider_qwen.evidence.ledger import EvidenceLedger
from spider_qwen.tools.fetch_service import FetchService, MockFetchProvider
from spider_qwen.tools.provider_types import FetchResult
from spider_qwen.tools.search_service import MockSearchProvider, SearchService


def _recipe(
    *, version: int = 1, required_text: list[str] | None = None,
    drift: RecipeDriftGuard | None = None,
) -> RetrievalRecipe:
    return RetrievalRecipe(
        recipe_id="quotation-pilot",
        version=version,
        preconditions=RecipePreconditions(
            modes=["service_quote_required"], query_terms_all=["cleaning"],
        ),
        steps=[
            RecipeStep(action="search", query_template="{query} {country} quotation", limit=3),
            RecipeStep(action="fetch", source="search_results", limit=2),
        ],
        postconditions=RecipeEvidenceConditions(
            min_fetched_pages=1,
            required_text_any=required_text or ["quotation"],
        ),
        drift_guard=drift or RecipeDriftGuard(),
        budget=RecipeBudget(max_search_calls=1, max_fetch_urls=2),
        promotion=PromotionPolicy(min_heldout_successes=3, min_independent_judges=2),
    )


def _services(*, query: str, search_fixtures: dict, fetch_fixtures: dict,
              budget: Budget | None = None):
    tracker = BudgetTracker(budget or Budget(
        mode="service_quote_required", max_search_calls=3, max_fetch_urls=6,
    ))
    ledger = EvidenceLedger("run_recipe")
    search = SearchService(MockSearchProvider(search_fixtures), ledger, tracker)
    fetch = FetchService(MockFetchProvider(fetch_fixtures), ledger, tracker, query=query)
    return tracker, search, fetch


def _run(pilot: RecipePilot, *, tracker, search, fetch, query="office cleaning",
         baseline_pages: list | None = None):
    return asyncio.run(pilot.run_shadow(
        run_id="run_recipe", query=query, mode="service_quote_required",
        country="Singapore", search=search, fetch=fetch, tracker=tracker,
        baseline_pages=baseline_pages or [FetchResult(url="https://baseline.example/service")],
    ))


def test_recipe_contract_allows_only_bounded_declarative_search_and_fetch():
    with pytest.raises(ValidationError, match="plain.*query.*country"):
        RecipeStep(action="search", query_template="{query.__class__}")
    with pytest.raises(ValidationError, match="fetch step"):
        RecipeStep(action="fetch", source="search_results", query_template="run()")
    with pytest.raises(ValidationError, match="extra_forbidden"):
        RecipeStep.model_validate({
            "action": "fetch", "source": "search_results", "limit": 1,
            "script": "website_code()",
        })
    with pytest.raises(ValidationError, match="Fetch step limits exceed"):
        RetrievalRecipe(
            recipe_id="over-budget", version=1,
            steps=[
                RecipeStep(action="search", query_template="{query}"),
                RecipeStep(action="fetch", source="search_results", limit=3),
            ],
            budget=RecipeBudget(max_search_calls=1, max_fetch_urls=2),
        )


def test_preconditions_are_explicit_and_conservative():
    conditions = _recipe().preconditions
    assert conditions.matches(
        query="Office CLEANING contract", mode="service_quote_required", country="Singapore",
    )
    assert not conditions.matches(
        query="printer toner", mode="service_quote_required", country="Singapore",
    )
    assert not conditions.matches(
        query="office cleaning", mode="product_exact_price", country="Singapore",
    )


def test_shadow_recipe_checks_fetched_evidence_and_records_divergence():
    query = "office cleaning Singapore quotation"
    recipe_url = "https://supplier.example/quote"
    tracker, search, fetch = _services(
        query=query,
        search_fixtures={query: [{"url": recipe_url, "title": "Supplier quote"}]},
        fetch_fixtures={recipe_url: {"text": "Request a quotation by email."}},
    )
    result = _run(RecipePilot([_recipe()]), tracker=tracker, search=search, fetch=fetch)
    execution = result["executions"][0]

    assert result["candidate_outputs_affected"] is False
    assert execution["outcome"] == "passed"
    assert execution["postconditions"] == {
        "min_fetched_pages": True,
        "evidence_refs": True,
        "required_text_any": True,
        "required_url_any": True,
    }
    assert execution["resource_usage"] == {"search_calls": 1, "fetch_urls": 1}
    assert execution["evidence_refs"]
    assert execution["divergence"]["recipe_only_urls"] == [recipe_url]
    assert execution["divergence"]["baseline_only_urls"] == [
        "https://baseline.example/service"
    ]


def test_divergence_preserves_case_sensitive_url_paths_and_queries():
    query = "office cleaning Singapore quotation"
    recipe_url = "https://Supplier.Example/QuoteCase?SKU=AbC"
    baseline_url = "https://supplier.example/quotecase?SKU=AbC"
    tracker, search, fetch = _services(
        query=query, search_fixtures={query: [{"url": recipe_url}]},
        fetch_fixtures={recipe_url: {"text": "Request a quotation."}},
    )
    execution = _run(
        RecipePilot([_recipe()]), tracker=tracker, search=search, fetch=fetch,
        baseline_pages=[FetchResult(url=baseline_url)],
    )["executions"][0]

    assert execution["divergence"]["recipe_only_urls"] == [
        "https://supplier.example/QuoteCase?SKU=AbC"
    ]
    assert execution["divergence"]["baseline_only_urls"] == [baseline_url]


def test_recipe_fetches_only_http_search_results():
    query = "office cleaning Singapore quotation"
    safe = "https://supplier.example/quote"
    tracker, search, fetch = _services(
        query=query,
        search_fixtures={query: [
            {"url": "javascript:alert(document.cookie)"},
            {"url": "file:///etc/passwd"},
            {"url": safe},
        ]},
        fetch_fixtures={safe: {"text": "Request a quotation."}},
    )
    execution = _run(
        RecipePilot([_recipe()]), tracker=tracker, search=search, fetch=fetch,
    )["executions"][0]

    assert execution["outcome"] == "passed"
    assert execution["fetched_urls"] == [safe]


def test_failed_fetched_postcondition_quarantines_version_and_skips_next_run(tmp_path):
    query = "office cleaning Singapore quotation"
    url = "https://supplier.example/quote"
    store = RecipeStateStore(tmp_path)
    pilot = RecipePilot([_recipe(required_text=["purchase order portal"])], store)
    tracker, search, fetch = _services(
        query=query, search_fixtures={query: [{"url": url}]},
        fetch_fixtures={url: {"text": "Generic company landing page."}},
    )

    first = _run(pilot, tracker=tracker, search=search, fetch=fetch)["executions"][0]
    assert first["outcome"] == "postcondition_failed"
    assert first["recipe_status"] == "quarantined"
    assert first["quarantine_reason"] == "failed postconditions: required_text_any"
    assert RecipeStateStore(tmp_path).reports(pilot.recipes[0])[0].outcome == "postcondition_failed"

    tracker2, search2, fetch2 = _services(query=query, search_fixtures={}, fetch_fixtures={})
    second = _run(pilot, tracker=tracker2, search=search2, fetch=fetch2)["executions"][0]
    assert second["outcome"] == "skipped_quarantined"
    assert tracker2.search_calls == tracker2.fetch_urls == 0

    # Quarantine is scoped to the immutable id/version pair.
    status, reason = store.ensure_recipe(_recipe(version=2))
    assert (status, reason) == ("shadow", None)


def test_drift_guard_and_same_version_definition_drift_quarantine(tmp_path):
    query = "office cleaning Singapore quotation"
    url = "https://unexpected.example/quote"
    store = RecipeStateStore(tmp_path)
    recipe = _recipe(drift=RecipeDriftGuard(allowed_hosts=["approved.example"]))
    tracker, search, fetch = _services(
        query=query, search_fixtures={query: [{"url": url}]},
        fetch_fixtures={url: {"text": "Quotation requests welcome."}},
    )
    execution = _run(
        RecipePilot([recipe], store), tracker=tracker, search=search, fetch=fetch,
    )["executions"][0]
    assert execution["outcome"] == "drift_detected"
    assert execution["drift_reasons"] == ["unexpected fetched host(s): unexpected.example"]

    original = _recipe(version=3)
    assert store.ensure_recipe(original)[0] == "shadow"
    changed = original.model_copy(update={"fallback": "none"})
    assert store.ensure_recipe(changed) == (
        "quarantined", "recipe definition changed without incrementing version",
    )


def test_global_budget_limit_falls_back_without_quarantine():
    recipe = _recipe()
    tracker, search, fetch = _services(
        query="office cleaning Singapore quotation", search_fixtures={}, fetch_fixtures={},
        budget=Budget(mode="service_quote_required", max_search_calls=1, max_fetch_urls=2),
    )
    tracker.consume_search()  # baseline used the full hard search cap
    store = RecipeStateStore()
    execution = _run(
        RecipePilot([recipe], store), tracker=tracker, search=search, fetch=fetch,
    )["executions"][0]

    assert execution["outcome"] == "budget_limited"
    assert execution["fallback_policy"] == "normal_pipeline"
    assert execution["baseline_retained"] is True
    assert store.ensure_recipe(recipe) == ("shadow", None)


def test_promotion_requires_repeated_heldout_successes_and_independent_judges(tmp_path):
    recipe = _recipe()
    store = RecipeStateStore(tmp_path)
    store.record_judgement(recipe, RecipeJudgement(
        heldout_task_id="task-1", judge_id="judge-a", success=True,
        independently_judged=True, qualification_verified=True,
        baseline_quality_preserved=True, resource_improvement_verified=True,
    ))
    store.record_judgement(recipe, RecipeJudgement(
        heldout_task_id="provisional", judge_id="model-first-pass", success=True,
    ))
    store.record_judgement(recipe, RecipeJudgement(
        heldout_task_id="task-2", judge_id="judge-a", success=True,
        independently_judged=True, qualification_verified=True,
        baseline_quality_preserved=True, resource_improvement_verified=True,
    ))
    with pytest.raises(ValueError, match="3 distinct held-out successes"):
        store.promote(recipe)

    store.record_judgement(recipe, RecipeJudgement(
        heldout_task_id="task-3", judge_id="judge-b", success=True,
        independently_judged=True, qualification_verified=True,
        baseline_quality_preserved=True, resource_improvement_verified=True,
    ))
    for task in ("task-1", "task-2", "task-3"):
        store.record_report(RecipeExecutionReport(
            run_id=task, recipe_id=recipe.recipe_id, recipe_version=recipe.version,
            outcome="passed", fallback_policy=recipe.fallback,
            resource_usage={"search_calls": 1, "fetch_urls": 2},
            postconditions={"heldout_evidence_gate": True},
        ))
    store.promote(recipe)
    reloaded = RecipeStateStore(tmp_path)
    assert reloaded.ensure_recipe(recipe) == ("promoted", None)
    assert any(j.heldout_task_id == "provisional" and not j.independently_judged
               for j in reloaded.judgements(recipe))

    failed = _recipe(version=2)
    for task, judge, success in [
        ("one", "a", True), ("two", "b", True), ("three", "b", True),
        ("four", "a", False),
    ]:
        store.record_judgement(failed, RecipeJudgement(
            heldout_task_id=task, judge_id=judge, success=success,
            independently_judged=True, qualification_verified=True,
            baseline_quality_preserved=True, resource_improvement_verified=True,
        ))
        store.record_report(RecipeExecutionReport(
            run_id=task, recipe_id=failed.recipe_id, recipe_version=failed.version,
            outcome="passed", fallback_policy=failed.fallback,
            resource_usage={"search_calls": 1, "fetch_urls": 2},
            postconditions={"heldout_evidence_gate": True},
        ))
    with pytest.raises(ValueError, match="held-out failure"):
        store.promote(failed)


def test_controller_shadow_recipe_uses_only_leftover_budget_and_preserves_candidates():
    data = deepcopy(load_policy().data)
    data["retrieval_recipes"] = {"enabled": False}
    data["budgets"]["service_quote_required"].update({
        "max_search_calls": 2,
        "max_fetch_urls": 7,
        "max_candidates_to_extract": 5,
        "max_validated_candidates": 5,
        "min_validated_candidates": 1,
    })
    policy = Policy(data)
    kwargs = dict(
        policy=policy, search_provider=MockSearchProvider(),
        fetch_provider=MockFetchProvider(), state_dir=None, persist=False,
    )
    baseline = asyncio.run(Controller(**kwargs).run(
        "office cleaning Singapore", mode="service_quote_required",
    ))
    with_recipe = asyncio.run(Controller(
        **kwargs, recipe_pilot=RecipePilot([_recipe()]),
    ).run("office cleaning Singapore", mode="service_quote_required"))

    def candidate_projection(result):
        return [
            {key: candidate.get(key) for key in (
                "supplier_id", "offering_id", "vendor_name", "website", "service_match",
            )}
            for candidate in result.validated_candidates
        ]

    assert candidate_projection(with_recipe) == candidate_projection(baseline)
    assert with_recipe.retrieval_recipes["candidate_outputs_affected"] is False
    execution = with_recipe.retrieval_recipes["executions"][0]
    assert execution["outcome"] == "passed"
    assert with_recipe.budget["search_calls"] <= with_recipe.budget["max_search_calls"]
    assert with_recipe.budget["fetch_urls"] <= with_recipe.budget["max_fetch_urls"]
    assert with_recipe.metrics["candidates_considered"] == baseline.metrics["candidates_considered"]


def test_policy_flag_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("SPIDER_QWEN_RETRIEVAL_RECIPES_ENABLED", raising=False)
    assert load_policy().retrieval_recipes_enabled() is False


def test_shadow_fetch_does_not_invoke_secondary_provider_on_degraded_page():
    class SecondaryFetcher:
        provider_name = "secondary"

        def __init__(self):
            self.calls = 0

        async def fetch(self, urls, output_format="markdown", include_links=True):
            self.calls += 1
            raise AssertionError("secondary fetch must remain disabled in recipe shadow")

    query = "office cleaning Singapore quotation"
    url = "https://supplier.example/degraded"
    tracker = BudgetTracker(Budget(
        mode="service_quote_required", max_search_calls=2, max_fetch_urls=4,
    ))
    ledger = EvidenceLedger("run_degraded")
    search = SearchService(MockSearchProvider({query: [{"url": url}]}), ledger, tracker)
    secondary = SecondaryFetcher()
    fetch = FetchService(
        MockFetchProvider({url: {"status": 500}}), ledger, tracker,
        query="office cleaning", fallback=secondary,
    )
    execution = _run(
        RecipePilot([_recipe()]), tracker=tracker, search=search, fetch=fetch,
    )["executions"][0]

    assert execution["outcome"] == "postcondition_failed"
    assert secondary.calls == 0
    assert execution["verified_output_claims"] is False
    assert execution["evidence_gate_scope"] == "retrieval_shape"
