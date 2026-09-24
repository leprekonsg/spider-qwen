"""Checkpointed requirement refresh for identified suppliers and approved pages.

Run with ``python -m spider_qwen.application.requirement_research --help``.
This never discovers suppliers, submits enquiries, or upgrades run readiness.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..evidence.ledger import EvidenceLedger
from ..modes.contracts import ProductCandidate, ServiceCandidate
from ..requirements import ProcurementRequest, Requirement, assess_requirements
from ..tools.fetch_service import MockFetchProvider, TinyFishFetchProvider
from .run_service import owner_state_dir


class RequirementResearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_kind: str = Field(pattern="^(service|product)$")
    candidate: dict
    requirement: Requirement
    approved_origins: list[str] = Field(min_length=1, max_length=20)
    urls: list[str] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def validate_scope(self):
        candidate = self.parsed_candidate()
        # Identity is supplied by the caller's completed discovery, never inferred
        # from a newly retrieved page or silently replaced with a generated key.
        if not self.candidate.get("supplier_id") or not self.candidate.get("offering_id"):
            raise ValueError("Supply the discovered supplier_id and offering_id.")
        request = self.request()
        origins = {self.origin(url) for url in request.supplier_sources[candidate.vendor_name]}
        if self.origin(candidate.website or "") not in origins:
            raise ValueError("The candidate website must have confirmed ownership in approved_origins.")
        for url in self.urls:
            parsed = urlsplit(url)
            if parsed.username or parsed.password or parsed.fragment or self.origin(url) not in origins:
                raise ValueError("Research URLs must belong to approved_origins, without credentials or fragments.")
        if len(set(self.urls)) != len(self.urls):
            raise ValueError("Remove duplicate research URLs.")
        return self

    @staticmethod
    def origin(url: str) -> tuple[str, str, int | None]:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Use an absolute HTTP(S) website URL.")
        return parsed.scheme, parsed.hostname.casefold(), parsed.port or (443 if parsed.scheme == "https" else 80)

    def parsed_candidate(self):
        cls = ServiceCandidate if self.candidate_kind == "service" else ProductCandidate
        candidate = cls.model_validate(self.candidate)
        # Refresh assesses new evidence only; no stale discovery/memory assertions.
        candidate.evidence_refs = []
        return candidate

    def request(self):
        return ProcurementRequest(
            query=self.requirement.text, requirements=[self.requirement],
            requirements_confirmed=True,
            supplier_sources={self.candidate["vendor_name"]: self.approved_origins},
        )


class RequirementResearchHandler:
    def __init__(self, state_dir: str | Path, *, offline: bool = True,
                 fixtures: dict | None = None, max_cost_micros_per_fetch: int = 0):
        if type(max_cost_micros_per_fetch) is not int or max_cost_micros_per_fetch < 0:
            raise ValueError("max_cost_micros_per_fetch must be a non-negative integer.")
        if not offline and max_cost_micros_per_fetch == 0:
            raise ValueError("Live research requires a positive per-fetch upper cost bound in USD millionths.")
        self.state_dir = Path(state_dir)
        self.offline = offline
        self.fixtures = fixtures
        self.max_cost = max_cost_micros_per_fetch

    def __call__(self, item, context):
        from .entity_research import EntityResearchClaim

        payload = RequirementResearchInput.model_validate(item.input_payload)
        candidate = payload.parsed_candidate()
        if (candidate.supplier_id, candidate.offering_id, payload.requirement.requirement_id) != (
            item.entity_id, item.offering_id, item.requirement_id
        ):
            raise ValueError("Research payload identities must match the checkpointed work item.")
        ledger = EvidenceLedger(
            f"research_{item.work_id}_{item.attempts}",
            state_dir=owner_state_dir(self.state_dir, context.owner),
        )

        async def fetch_pages():
            client = None
            if self.offline:
                provider = MockFetchProvider(fixtures=self.fixtures)
            else:
                from ..tools.tinyfish_client import from_env
                client = from_env()
                # Targeted work-item retry is the only retry layer; each reserved
                # operation makes exactly one provider request for one URL.
                client.max_retries = 0
                provider = TinyFishFetchProvider(client=client)
            # This path calls the provider directly (one metered request per
            # URL), so it applies the v1 allowlist itself.
            from ..agent.tool_registry import ToolRegistry

            ToolRegistry.require_allowed("fetch", provider.fetch_source_tool)
            try:
                for url in payload.urls:
                    result = await context.acquire_async(
                        lambda: provider.fetch([url]), provider=provider.provider_name,
                        provider_calls=1, max_cost_micros=self.max_cost,
                    )
                    if result.errors or not result.results:
                        raise RuntimeError("Page retrieval failed; retry this work item after checking the source.")
                    approved = {payload.origin(origin) for origin in payload.approved_origins}
                    for page in result.results:
                        final_url = page.final_url or page.url
                        final = urlsplit(final_url)
                        if (page.url != url or final.username or final.password or final.fragment
                                or payload.origin(final_url) not in approved):
                            raise ValueError("Retrieved page left the approved supplier origins; review ownership before retrying.")
                        ref = ledger.record(
                            source_tool=page.source_tool, url=page.url,
                            final_url=page.final_url, title=page.title,
                            text=page.text, snippet=page.text[:4000], language=page.language,
                        )
                        candidate.evidence_refs.append(ref)
            finally:
                if client is not None:
                    await client.aclose()

        try:
            asyncio.run(fetch_pages())
            assessment = assess_requirements(payload.request(), candidate, ledger)[0]
            return EntityResearchClaim(
                entity_id=item.entity_id, offering_id=item.offering_id,
                requirement_id=item.requirement_id, status=assessment.status,
                evidence_refs=assessment.evidence_refs, observed_at=assessment.observed_at,
                reason=assessment.reason, excerpts=assessment.excerpts,
            )
        finally:
            # Partial evidence survives provider failure; completed claims are
            # committed only by the runner after the handler returns successfully.
            ledger.persist()


def main(argv=None):
    from .entity_research import EntityResearchError, EntityResearchRunner, EntityResearchTask

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request", type=Path, help="JSON dataset_id, input_version, tasks, limits")
    parser.add_argument("--state-dir", default=".spider_qwen")
    parser.add_argument("--owner", default="local", help="Local operator scope; not a remote authentication mechanism")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--fixtures", type=Path, help="Offline URL-to-page fixture JSON")
    parser.add_argument("--max-cost-micros-per-fetch", type=int, default=0)
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args(argv)
    if args.live and os.getenv("SPIDER_QWEN_ALLOW_LIVE") != "1":
        parser.error("Live research requires SPIDER_QWEN_ALLOW_LIVE=1 and an explicit per-fetch cost bound.")
    if args.live and args.fixtures:
        parser.error("--fixtures applies only to offline research.")
    try:
        data = json.loads(args.request.read_text(encoding="utf-8"))
        tasks = []
        for raw in data["tasks"]:
            payload = RequirementResearchInput.model_validate(raw)
            candidate = payload.parsed_candidate()
            tasks.append(EntityResearchTask(
                entity_id=candidate.supplier_id, offering_id=candidate.offering_id,
                requirement_id=payload.requirement.requirement_id,
                input_payload=payload.model_dump(mode="json"),
            ))
        handler = RequirementResearchHandler(
            args.state_dir, offline=not args.live,
            fixtures=json.loads(args.fixtures.read_text(encoding="utf-8")) if args.fixtures else None,
            max_cost_micros_per_fetch=args.max_cost_micros_per_fetch,
        )
        runner = EntityResearchRunner(args.state_dir, handler)
        scope = dict(owner=args.owner, dataset_id=data["dataset_id"], input_version=data["input_version"])
        try:
            runner.submit(**scope, tasks=tasks, limits=data["limits"])
            if args.retry_failed:
                runner.retry_failed(**scope)
            status = runner.run_pending(**scope)
            print(json.dumps({"offline": not args.live, "status": status,
                              "results": [claim.model_dump(mode="json") for claim in runner.results(**scope)]}, indent=2))
        finally:
            runner.close()
    except (ValueError, KeyError, OSError, EntityResearchError) as exc:
        parser.error(str(exc))
    return 1 if status["work_items"]["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
