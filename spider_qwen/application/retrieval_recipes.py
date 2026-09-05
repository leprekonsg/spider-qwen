"""Application-owned, evidence-gated retrieval recipe pilot.

Recipes are declarative search/fetch plans.  They can only interpolate the
current request and country, and fetch steps can only consume URLs returned by
the recipe's own searches.  The pilot always executes in shadow mode: fetched
pages are recorded for evaluation but never enter candidate extraction.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from string import Formatter
from typing import Any, Literal
from urllib.parse import urlparse, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


RecipeStatus = Literal["shadow", "promoted", "quarantined"]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _normalized_url(url: str) -> str:
    parts = urlsplit((url or "").strip())
    path = "" if parts.path == "/" else parts.path
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), path, parts.query, ""))


def _safe_web_url(url: str) -> bool:
    parts = urlsplit(url or "")
    return parts.scheme.casefold() in {"http", "https"} and bool(parts.hostname)


class RecipePreconditions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    modes: list[str] = Field(default_factory=list, max_length=3)
    query_terms_any: list[str] = Field(default_factory=list, max_length=20)
    query_terms_all: list[str] = Field(default_factory=list, max_length=20)
    countries: list[str] = Field(default_factory=list, max_length=20)

    def matches(self, *, query: str, mode: str, country: str | None) -> bool:
        query_value = query.casefold()
        if self.modes and mode not in self.modes:
            return False
        if self.countries and (country or "").casefold() not in {
            value.casefold() for value in self.countries
        }:
            return False
        if self.query_terms_any and not any(
            term.casefold() in query_value for term in self.query_terms_any
        ):
            return False
        return all(term.casefold() in query_value for term in self.query_terms_all)


class RecipeStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["search", "fetch"]
    query_template: str | None = Field(default=None, min_length=1, max_length=500)
    source: Literal["search_results"] | None = None
    limit: int = Field(default=5, ge=1, le=20)

    @model_validator(mode="after")
    def validate_shape(self):
        if self.action == "search":
            if not self.query_template or self.source is not None:
                raise ValueError("A search step requires query_template and cannot set source.")
            try:
                parsed = list(Formatter().parse(self.query_template))
            except ValueError as exc:
                raise ValueError("query_template contains malformed braces.") from exc
            for _literal, field, format_spec, conversion in parsed:
                if field is None:
                    continue
                if field not in {"query", "country"} or format_spec or conversion:
                    raise ValueError(
                        "query_template may only use plain {query} and {country} placeholders."
                    )
        elif self.query_template is not None or self.source != "search_results":
            raise ValueError(
                "A fetch step must set source='search_results' and cannot set query_template."
            )
        return self


class RecipeEvidenceConditions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_fetched_pages: int = Field(default=1, ge=1, le=20)
    require_evidence_refs: bool = True
    required_text_any: list[str] = Field(default_factory=list, max_length=30)
    required_url_any: list[str] = Field(default_factory=list, max_length=30)


class RecipeDriftGuard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_hosts: list[str] = Field(default_factory=list, max_length=30)
    required_text_any: list[str] = Field(default_factory=list, max_length=30)


class RecipeBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_search_calls: int = Field(default=1, ge=1, le=3)
    max_fetch_urls: int = Field(default=2, ge=1, le=10)


class PromotionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_heldout_successes: int = Field(default=3, ge=2, le=100)
    min_independent_judges: int = Field(default=2, ge=2, le=20)


class RetrievalRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recipe_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")
    version: int = Field(ge=1)
    preconditions: RecipePreconditions = Field(default_factory=RecipePreconditions)
    steps: list[RecipeStep] = Field(min_length=2, max_length=6)
    postconditions: RecipeEvidenceConditions = Field(default_factory=RecipeEvidenceConditions)
    drift_guard: RecipeDriftGuard = Field(default_factory=RecipeDriftGuard)
    budget: RecipeBudget = Field(default_factory=RecipeBudget)
    fallback: Literal["normal_pipeline", "none"] = "normal_pipeline"
    promotion: PromotionPolicy = Field(default_factory=PromotionPolicy)

    @model_validator(mode="after")
    def validate_plan(self):
        searches = [step for step in self.steps if step.action == "search"]
        fetches = [step for step in self.steps if step.action == "fetch"]
        if not searches or not fetches:
            raise ValueError("A retrieval recipe requires at least one search and one fetch step.")
        if len(searches) > self.budget.max_search_calls:
            raise ValueError("Search steps exceed recipe budget.max_search_calls.")
        if sum(step.limit for step in fetches) > self.budget.max_fetch_urls:
            raise ValueError("Fetch step limits exceed recipe budget.max_fetch_urls.")
        if self.postconditions.min_fetched_pages > self.budget.max_fetch_urls:
            raise ValueError("postconditions.min_fetched_pages exceeds the recipe fetch budget.")
        search_seen = False
        for step in self.steps:
            if step.action == "search":
                search_seen = True
            elif not search_seen:
                raise ValueError("Each fetch step must follow a search step.")
        return self

    @property
    def definition_hash(self) -> str:
        body = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(body.encode("utf-8")).hexdigest()


class RecipeJudgement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    heldout_task_id: str = Field(min_length=1, max_length=128)
    judge_id: str = Field(min_length=1, max_length=128)
    success: bool
    # Provisional/model labels must opt in explicitly after independent review.
    independently_judged: bool = False
    # The judge must affirm downstream supplier qualification, not merely that
    # a page contained a recipe keyword.
    qualification_verified: bool = False
    baseline_quality_preserved: bool = False
    resource_improvement_verified: bool = False
    note: str = Field(default="", max_length=1000)


class RecipeDivergence(BaseModel):
    baseline_only_urls: list[str] = Field(default_factory=list)
    recipe_only_urls: list[str] = Field(default_factory=list)
    overlapping_urls: list[str] = Field(default_factory=list)


class RecipeExecutionReport(BaseModel):
    execution_id: str = Field(default_factory=lambda: f"recipe_{uuid.uuid4().hex[:16]}")
    run_id: str
    recipe_id: str
    recipe_version: int
    recipe_status: RecipeStatus = "shadow"
    execution_mode: Literal["shadow"] = "shadow"
    outcome: Literal[
        "passed", "postcondition_failed", "drift_detected", "budget_limited",
        "error", "skipped_quarantined",
    ]
    queries: list[str] = Field(default_factory=list)
    fetched_urls: list[str] = Field(default_factory=list)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    resource_usage: dict[str, int] = Field(default_factory=dict)
    postconditions: dict[str, bool] = Field(default_factory=dict)
    evidence_gate_scope: Literal["retrieval_shape"] = "retrieval_shape"
    verified_output_claims: Literal[False] = False
    drift_reasons: list[str] = Field(default_factory=list)
    divergence: RecipeDivergence = Field(default_factory=RecipeDivergence)
    fallback_policy: Literal["normal_pipeline", "none"]
    baseline_retained: bool = True
    quarantine_reason: str | None = None
    error: str | None = None
    recorded_at: str = Field(default_factory=_utc_now)

    @model_validator(mode="after")
    def validate_outcome(self):
        if any(value < 0 for value in self.resource_usage.values()):
            raise ValueError("Recipe resource usage cannot be negative.")
        if self.outcome == "passed" and (
            not self.postconditions or not all(self.postconditions.values())
        ):
            raise ValueError("A passed recipe report requires all recorded postconditions to pass.")
        return self


class RecipeStateStore:
    """Durable recipe status, held-out judgements, and shadow reports."""

    def __init__(self, state_dir: str | Path | None = None) -> None:
        self._lock = threading.RLock()
        self._memory_states: dict[tuple[str, int], dict[str, str | None]] = {}
        self._memory_judgements: dict[tuple[str, int, str, str], RecipeJudgement] = {}
        self._memory_reports: list[RecipeExecutionReport] = []
        self.db_path: Path | None = None
        if state_dir is not None:
            target = Path(state_dir)
            target.mkdir(parents=True, exist_ok=True)
            self.db_path = target / "retrieval_recipes.sqlite3"
            self._init_db()

    def _connect(self) -> sqlite3.Connection:
        assert self.db_path is not None
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS recipe_state (
                    recipe_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    definition_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    quarantine_reason TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (recipe_id, version)
                );
                CREATE TABLE IF NOT EXISTS recipe_judgements (
                    recipe_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    heldout_task_id TEXT NOT NULL,
                    judge_id TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    independently_judged INTEGER NOT NULL,
                    qualification_verified INTEGER NOT NULL,
                    baseline_quality_preserved INTEGER NOT NULL,
                    resource_improvement_verified INTEGER NOT NULL,
                    note TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    PRIMARY KEY (recipe_id, version, heldout_task_id, judge_id)
                );
                CREATE TABLE IF NOT EXISTS recipe_execution_reports (
                    execution_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    recipe_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    report_json TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                """
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(recipe_judgements)")
            }
            if "qualification_verified" not in columns:
                connection.execute(
                    "ALTER TABLE recipe_judgements ADD COLUMN qualification_verified "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            if "baseline_quality_preserved" not in columns:
                connection.execute(
                    "ALTER TABLE recipe_judgements ADD COLUMN baseline_quality_preserved "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            if "resource_improvement_verified" not in columns:
                connection.execute(
                    "ALTER TABLE recipe_judgements ADD COLUMN resource_improvement_verified "
                    "INTEGER NOT NULL DEFAULT 0"
                )

    def ensure_recipe(self, recipe: RetrievalRecipe) -> tuple[RecipeStatus, str | None]:
        key = (recipe.recipe_id, recipe.version)
        now = _utc_now()
        with self._lock:
            if self.db_path is None:
                state = self._memory_states.get(key)
                if state is None:
                    state = {"definition_hash": recipe.definition_hash, "status": "shadow",
                             "quarantine_reason": None}
                    self._memory_states[key] = state
                elif state["definition_hash"] != recipe.definition_hash:
                    state.update(status="quarantined", quarantine_reason=(
                        "recipe definition changed without incrementing version"
                    ))
                return state["status"], state["quarantine_reason"]  # type: ignore[return-value]
            with self._connect() as connection:
                row = connection.execute(
                    "SELECT definition_hash,status,quarantine_reason FROM recipe_state "
                    "WHERE recipe_id=? AND version=?", key,
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO recipe_state VALUES (?,?,?,?,?,?)",
                        (*key, recipe.definition_hash, "shadow", None, now),
                    )
                    return "shadow", None
                if row["definition_hash"] != recipe.definition_hash:
                    reason = "recipe definition changed without incrementing version"
                    connection.execute(
                        "UPDATE recipe_state SET status='quarantined',quarantine_reason=?,updated_at=? "
                        "WHERE recipe_id=? AND version=?", (reason, now, *key),
                    )
                    return "quarantined", reason
                return row["status"], row["quarantine_reason"]

    def quarantine(self, recipe: RetrievalRecipe, reason: str) -> None:
        self.ensure_recipe(recipe)
        key = (recipe.recipe_id, recipe.version)
        with self._lock:
            if self.db_path is None:
                self._memory_states[key].update(status="quarantined", quarantine_reason=reason)
                return
            with self._connect() as connection:
                connection.execute(
                    "UPDATE recipe_state SET status='quarantined',quarantine_reason=?,updated_at=? "
                    "WHERE recipe_id=? AND version=?", (reason, _utc_now(), *key),
                )

    def record_judgement(self, recipe: RetrievalRecipe, judgement: RecipeJudgement) -> None:
        self.ensure_recipe(recipe)
        key = (recipe.recipe_id, recipe.version, judgement.heldout_task_id, judgement.judge_id)
        with self._lock:
            if self.db_path is None:
                self._memory_judgements[key] = judgement
                return
            with self._connect() as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO recipe_judgements "
                    "(recipe_id,version,heldout_task_id,judge_id,success,independently_judged,"
                    "qualification_verified,baseline_quality_preserved,resource_improvement_verified,"
                    "note,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (*key, int(judgement.success), int(judgement.independently_judged),
                     int(judgement.qualification_verified),
                     int(judgement.baseline_quality_preserved),
                     int(judgement.resource_improvement_verified), judgement.note, _utc_now()),
                )

    def promote(self, recipe: RetrievalRecipe) -> None:
        """Mark shadow results eligible; execution remains shadow-only."""
        status, reason = self.ensure_recipe(recipe)
        if status == "quarantined":
            raise ValueError(f"Cannot promote quarantined recipe: {reason}.")
        judgements = self.judgements(recipe)
        reports = self.reports(recipe)
        passed_runs = {
            report.run_id for report in reports
            if report.outcome == "passed"
            and report.resource_usage.get("search_calls", 0) <= recipe.budget.max_search_calls
            and report.resource_usage.get("fetch_urls", 0) <= recipe.budget.max_fetch_urls
        }
        independent = [j for j in judgements if j.independently_judged]
        failures = [j for j in independent if not j.success]
        eligible = [
            j for j in independent
            if (j.success and j.qualification_verified and j.baseline_quality_preserved
                and j.resource_improvement_verified and j.heldout_task_id in passed_runs)
        ]
        tasks = {j.heldout_task_id for j in eligible}
        judges = {j.judge_id for j in eligible}
        missing: list[str] = []
        if failures:
            missing.append(f"resolve {len(failures)} independently judged held-out failure(s)")
        if len(tasks) < recipe.promotion.min_heldout_successes:
            missing.append(
                f"record {recipe.promotion.min_heldout_successes} distinct held-out successes "
                "with a matched passed run and verified qualification, baseline quality, and "
                f"resource improvement ({len(tasks)} eligible)"
            )
        if len(judges) < recipe.promotion.min_independent_judges:
            missing.append(
                f"use {recipe.promotion.min_independent_judges} independent judges "
                f"({len(judges)} recorded)"
            )
        if missing:
            raise ValueError("Promotion requirements not met: " + "; ".join(missing) + ".")
        key = (recipe.recipe_id, recipe.version)
        with self._lock:
            if self.db_path is None:
                self._memory_states[key].update(status="promoted", quarantine_reason=None)
                return
            with self._connect() as connection:
                connection.execute(
                    "UPDATE recipe_state SET status='promoted',quarantine_reason=NULL,updated_at=? "
                    "WHERE recipe_id=? AND version=?", (_utc_now(), *key),
                )

    def judgements(self, recipe: RetrievalRecipe) -> list[RecipeJudgement]:
        key = (recipe.recipe_id, recipe.version)
        with self._lock:
            if self.db_path is None:
                return [value for stored_key, value in self._memory_judgements.items()
                        if stored_key[:2] == key]
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT heldout_task_id,judge_id,success,independently_judged,"
                    "qualification_verified,baseline_quality_preserved,"
                    "resource_improvement_verified,note "
                    "FROM recipe_judgements WHERE recipe_id=? AND version=?", key,
                ).fetchall()
        return [RecipeJudgement(
            heldout_task_id=row["heldout_task_id"], judge_id=row["judge_id"],
            success=bool(row["success"]), independently_judged=bool(row["independently_judged"]),
            qualification_verified=bool(row["qualification_verified"]), note=row["note"],
            baseline_quality_preserved=bool(row["baseline_quality_preserved"]),
            resource_improvement_verified=bool(row["resource_improvement_verified"]),
        ) for row in rows]

    def record_report(self, report: RecipeExecutionReport) -> None:
        with self._lock:
            if self.db_path is None:
                self._memory_reports.append(report)
                return
            with self._connect() as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO recipe_execution_reports VALUES (?,?,?,?,?,?)",
                    (report.execution_id, report.run_id, report.recipe_id, report.recipe_version,
                     report.model_dump_json(), report.recorded_at),
                )

    def reports(self, recipe: RetrievalRecipe) -> list[RecipeExecutionReport]:
        key = (recipe.recipe_id, recipe.version)
        with self._lock:
            if self.db_path is None:
                return [report for report in self._memory_reports
                        if (report.recipe_id, report.recipe_version) == key]
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT report_json FROM recipe_execution_reports "
                    "WHERE recipe_id=? AND version=? ORDER BY recorded_at", key,
                ).fetchall()
        return [RecipeExecutionReport.model_validate_json(row["report_json"]) for row in rows]


class RecipePilot:
    def __init__(self, recipes: list[RetrievalRecipe], store: RecipeStateStore | None = None) -> None:
        keys = [(recipe.recipe_id, recipe.version) for recipe in recipes]
        if len(keys) != len(set(keys)):
            raise ValueError("Each retrieval recipe id/version must be unique.")
        self.recipes = list(recipes)
        self.store = store or RecipeStateStore()

    async def run_shadow(
        self, *, run_id: str, query: str, mode: str, country: str | None,
        search: Any, fetch: Any, tracker: Any, baseline_pages: list[Any], tracer: Any = None,
    ) -> dict[str, Any]:
        reports: list[RecipeExecutionReport] = []
        fetch = self._without_secondary_fetchers(fetch)
        for recipe in self.recipes:
            if not recipe.preconditions.matches(query=query, mode=mode, country=country):
                continue
            reports.append(await self._execute(
                recipe, run_id=run_id, query=query, country=country, search=search,
                fetch=fetch, tracker=tracker, baseline_pages=baseline_pages, tracer=tracer,
            ))
        return {
            "enabled": True,
            "execution_mode": "shadow",
            "candidate_outputs_affected": False,
            "executions": [report.model_dump(mode="json") for report in reports],
        }

    async def _execute(
        self, recipe: RetrievalRecipe, *, run_id: str, query: str, country: str | None,
        search: Any, fetch: Any, tracker: Any, baseline_pages: list[Any], tracer: Any,
    ) -> RecipeExecutionReport:
        status, quarantine_reason = self.store.ensure_recipe(recipe)
        if status == "quarantined":
            report = RecipeExecutionReport(
                run_id=run_id, recipe_id=recipe.recipe_id, recipe_version=recipe.version,
                recipe_status="quarantined", outcome="skipped_quarantined",
                quarantine_reason=quarantine_reason, fallback_policy=recipe.fallback,
            )
            self.store.record_report(report)
            return report

        before_search = int(tracker.search_calls)
        before_fetch = int(tracker.fetch_urls)
        queries: list[str] = []
        discovered: list[str] = []
        pages: list[Any] = []
        outcome = "passed"
        error: str | None = None
        try:
            for step in recipe.steps:
                if step.action == "search":
                    local_searches = int(tracker.search_calls) - before_search
                    if local_searches >= recipe.budget.max_search_calls or not tracker.can_search():
                        outcome = "budget_limited"
                        break
                    rendered = step.query_template.format(
                        query=query, country=(country or "")
                    ).strip()
                    queries.append(rendered)
                    results = await search.search(
                        rendered, location=country, language="en", limit=step.limit,
                    )
                    for url in results.urls():
                        if _safe_web_url(url) and url not in discovered:
                            discovered.append(url)
                else:
                    local_fetches = int(tracker.fetch_urls) - before_fetch
                    global_remaining = max(0, tracker.budget.max_fetch_urls - tracker.fetch_urls)
                    local_remaining = recipe.budget.max_fetch_urls - local_fetches
                    allowed = min(step.limit, global_remaining, local_remaining)
                    if allowed <= 0:
                        outcome = "budget_limited"
                        break
                    targets = [url for url in discovered
                               if url not in {page.url for page in pages}][:allowed]
                    if not targets:
                        break
                    fetched = await fetch.fetch(targets)
                    pages.extend(fetched.results)
                    if allowed < step.limit:
                        outcome = "budget_limited"
                        break
        except Exception as exc:
            outcome = "error"
            error = f"{type(exc).__name__}: {exc}"

        baseline_urls = {_normalized_url(getattr(page, "final_url", None) or page.url)
                         for page in baseline_pages}
        recipe_urls = {_normalized_url(getattr(page, "final_url", None) or page.url)
                       for page in pages}
        divergence = RecipeDivergence(
            baseline_only_urls=sorted(baseline_urls - recipe_urls),
            recipe_only_urls=sorted(recipe_urls - baseline_urls),
            overlapping_urls=sorted(recipe_urls & baseline_urls),
        )
        postconditions = self._postconditions(recipe.postconditions, pages)
        drift_reasons = self._drift_reasons(recipe.drift_guard, pages)
        reason: str | None = None
        if outcome == "passed" and drift_reasons:
            outcome = "drift_detected"
            reason = "; ".join(drift_reasons)
        elif outcome == "passed" and not all(postconditions.values()):
            outcome = "postcondition_failed"
            failed = [name for name, passed in postconditions.items() if not passed]
            reason = "failed postconditions: " + ", ".join(failed)
        if reason:
            self.store.quarantine(recipe, reason)
            status = "quarantined"

        report = RecipeExecutionReport(
            run_id=run_id, recipe_id=recipe.recipe_id, recipe_version=recipe.version,
            recipe_status=status, outcome=outcome, queries=queries,
            fetched_urls=[getattr(page, "final_url", None) or page.url for page in pages],
            evidence_refs=[page.evidence_ref.model_dump(mode="json") for page in pages
                           if getattr(page, "evidence_ref", None) is not None],
            resource_usage={
                "search_calls": int(tracker.search_calls) - before_search,
                "fetch_urls": int(tracker.fetch_urls) - before_fetch,
            },
            postconditions=postconditions, drift_reasons=drift_reasons,
            divergence=divergence,
            fallback_policy=recipe.fallback,
            baseline_retained=True,
            quarantine_reason=reason, error=error,
        )
        self.store.record_report(report)
        if tracer is not None:
            tracer.record(
                step="retrieval_recipe_shadow", tool="retrieval_recipe",
                status="success" if outcome == "passed" else (
                    "blocked" if outcome in {"budget_limited", "skipped_quarantined"} else "error"
                ),
                input_count=len(recipe.steps), output_count=len(pages),
                detail={"outcome": outcome, **report.resource_usage}, error=error,
            )
        return report

    @staticmethod
    def _without_secondary_fetchers(fetch: Any) -> Any:
        """Clone the standard service without model/archive retry paths.

        Recipe budgets count primary search/fetch work. Disabling page judges,
        fallback extractors, and Wayback prevents hidden secondary calls and
        ensures fetched evidence alone determines the recipe shape checks.
        """
        from ..tools.fetch_service import FetchService

        if not isinstance(fetch, FetchService):
            return fetch
        return FetchService(
            fetch.provider, fetch.ledger, fetch.tracker, fetch.tracer,
            judge=None, query=fetch.query, wayback=None, cache=None, fallback=None,
        )

    @staticmethod
    def _postconditions(conditions: RecipeEvidenceConditions, pages: list[Any]) -> dict[str, bool]:
        texts = " ".join((getattr(page, "text", "") or "") for page in pages).casefold()
        urls = " ".join((getattr(page, "final_url", None) or page.url) for page in pages).casefold()
        return {
            "min_fetched_pages": len(pages) >= conditions.min_fetched_pages,
            "evidence_refs": (
                not conditions.require_evidence_refs
                or bool(pages) and all(getattr(page, "evidence_ref", None) is not None for page in pages)
            ),
            "required_text_any": (
                not conditions.required_text_any
                or any(term.casefold() in texts for term in conditions.required_text_any)
            ),
            "required_url_any": (
                not conditions.required_url_any
                or any(term.casefold() in urls for term in conditions.required_url_any)
            ),
        }

    @staticmethod
    def _drift_reasons(guard: RecipeDriftGuard, pages: list[Any]) -> list[str]:
        reasons: list[str] = []
        if guard.allowed_hosts:
            allowed = {host.casefold().lstrip(".") for host in guard.allowed_hosts}
            hosts = {
                (urlparse(getattr(page, "final_url", None) or page.url).hostname or "").casefold()
                for page in pages
            }
            unexpected = sorted(
                host for host in hosts
                if host and not any(host == expected or host.endswith("." + expected)
                                    for expected in allowed)
            )
            if unexpected:
                reasons.append("unexpected fetched host(s): " + ", ".join(unexpected))
        if guard.required_text_any:
            text = " ".join((getattr(page, "text", "") or "") for page in pages).casefold()
            if not any(term.casefold() in text for term in guard.required_text_any):
                reasons.append("required drift-guard text was absent")
        return reasons


def default_retrieval_recipes() -> list[RetrievalRecipe]:
    """Fixed pilot recipe owned by the application release."""
    return [RetrievalRecipe(
        recipe_id="official-quotation-channel",
        version=1,
        preconditions=RecipePreconditions(modes=[
            "service_quote_required", "contact_enrichment_only",
        ]),
        steps=[
            RecipeStep(action="search", query_template="{query} official quotation contact", limit=5),
            RecipeStep(action="fetch", source="search_results", limit=2),
        ],
        postconditions=RecipeEvidenceConditions(
            min_fetched_pages=1,
            required_text_any=["quote", "quotation", "contact", "email"],
        ),
        budget=RecipeBudget(max_search_calls=1, max_fetch_urls=2),
        fallback="normal_pipeline",
    )]


def build_default_recipe_pilot(state_dir: str | Path | None) -> RecipePilot:
    return RecipePilot(default_retrieval_recipes(), RecipeStateStore(state_dir))
