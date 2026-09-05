"""Shared, read-only projections of owner-scoped completed runs."""

from __future__ import annotations

from copy import deepcopy
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

Operation = Literal[
    "get_current_run", "list_candidates", "get_candidate_evidence",
    "compare_candidates", "get_rfq_draft",
]


class InspectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supplier_id: str | None = Field(default=None, min_length=1, max_length=128)
    supplier_ids: list[str] = Field(default_factory=list, max_length=10)
    offering_id: str | None = Field(default=None, min_length=1, max_length=128)
    offering_ids: list[str] = Field(default_factory=list, max_length=10)


class CompletedRunQueries:
    def __init__(self, result_loader: Callable, evidence_loader: Callable | None = None):
        self._load = result_loader
        self._evidence = evidence_loader

    def inspect(self, run_id: str, operation: Operation, *, owner: str,
                supplier_id: str | None = None, supplier_ids: list[str] | None = None,
                offering_id: str | None = None, offering_ids: list[str] | None = None) -> dict:
        # The loader enforces ownership and completion before any projection.
        result = self._load(run_id, owner=owner)
        candidates = result.get("validated_candidates", [])
        withheld = result.get("withheld_candidates", [])

        def candidate(supplier: str | None = None, offering: str | None = None) -> dict:
            matches = [
                c for c in candidates + withheld
                if (not supplier or c.get("supplier_id") == supplier)
                and (not offering or c.get("offering_id") == offering)
                and (supplier or offering)
            ]
            if len(matches) != 1:
                raise ValueError(
                    "Select one candidate from list_candidates using supplier_id and, "
                    "when that supplier has multiple offerings, offering_id."
                )
            return matches[0]

        if operation == "get_current_run":
            return {key: deepcopy(result[key]) for key in (
                "run_id", "query", "mode", "stop_reason", "schema_version",
                "execution", "profile", "pipeline_version", "effective_config",
                "procurement_request", "qualification_summary",
                "retrieval_recipes",
            ) if key in result}
        if operation == "list_candidates":
            return {"run_id": run_id, "candidates": deepcopy(candidates),
                    "withheld_candidates": deepcopy(withheld)}
        if operation == "get_candidate_evidence":
            selected = candidate(supplier_id, offering_id)
            ids = {ref["ledger_id"] for ref in selected.get("evidence_refs", [])}
            return {"run_id": run_id, "supplier_id": selected.get("supplier_id"),
                    "offering_id": selected.get("offering_id"),
                    "offer_scope": deepcopy(selected.get("offer_scope")),
                    "offer_scope_status": selected.get("offer_scope_status"),
                    "offer_observations": deepcopy(
                        selected.get("field_claims", {}).get("offer_scope", [])
                    ),
                    "evidence_refs": deepcopy(selected.get("evidence_refs", [])),
                    "field_claims": deepcopy(selected.get("field_claims", {})),
                    "conflicting_fields": deepcopy(selected.get("conflicting_fields", [])),
                    "readiness": deepcopy(selected.get("readiness")),
                    "qualification": deepcopy(selected.get("qualification")),
                    "requirement_assessments": deepcopy(selected.get("requirement_assessments", [])),
                    "observations": self._evidence(run_id, owner=owner, ledger_ids=ids) if self._evidence else [],
                    "citation_proofs": deepcopy([p for p in result.get("citation_proofs", [])
                                                 if p.get("ledger_id") in ids])}
        if operation == "compare_candidates":
            selected_offerings = offering_ids or []
            selected_suppliers = supplier_ids or []
            if selected_offerings and selected_suppliers:
                raise ValueError("Compare using offering_ids or supplier_ids, not both.")
            identifiers = selected_offerings or selected_suppliers
            if not 2 <= len(identifiers) <= 10 or len(set(identifiers)) != len(identifiers):
                raise ValueError("Select 2 to 10 distinct offering_ids or supplier_ids from this run.")
            rows = (
                [candidate(offering=identifier) for identifier in identifiers]
                if selected_offerings else [candidate(supplier=identifier) for identifier in identifiers]
            )
            return {"run_id": run_id, "candidates": deepcopy(rows)}
        if operation == "get_rfq_draft":
            selected = candidate(supplier_id, offering_id)
            drafts = result.get("rfq_drafts", [])
            matches = [
                d for d in drafts
                if d.get("vendor", {}).get("supplier_id") == selected.get("supplier_id")
                and (
                    not selected.get("offering_id")
                    or d.get("vendor", {}).get("offering_id") == selected.get("offering_id")
                )
            ]
            # Legacy results may lack draft IDs. Require both name and website,
            # and never choose arbitrarily among same-name suppliers.
            if not matches:
                matches = [d for d in drafts if not d.get("vendor", {}).get("supplier_id")
                           and d.get("vendor", {}).get("vendor_name") == selected.get("vendor_name")
                           and d.get("vendor", {}).get("website") == selected.get("website")]
            if len(matches) > 1:
                raise ValueError("Multiple drafts match this supplier; inspect the run result directly.")
            return {"run_id": run_id, "supplier_id": selected.get("supplier_id"),
                    "offering_id": selected.get("offering_id"), "submission_status": "unsent",
                    "draft": deepcopy(matches[0]) if matches else None}
        raise ValueError("Unknown read-only run operation.")


def register_query_routes(app, service, owner_dependency):
    from fastapi import Depends, HTTPException

    def evidence(run_id, *, owner, ledger_ids):
        return load_candidate_observations(service.state_dir, run_id, owner=owner, ledger_ids=ledger_ids)
    queries = CompletedRunQueries(service.result, evidence)

    @app.post("/runs/{run_id}/inspect/{operation}")
    def inspect_run(run_id: str, operation: Operation, req: InspectRequest,
                    owner: str = Depends(owner_dependency)):
        try:
            return queries.inspect(run_id, operation, owner=owner,
                                   supplier_id=req.supplier_id, supplier_ids=req.supplier_ids,
                                   offering_id=req.offering_id, offering_ids=req.offering_ids)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


def load_candidate_observations(state_dir, run_id: str, *, owner: str, ledger_ids: set[str]) -> list[dict]:
    """Read bounded excerpts after checking completed-run ownership."""
    from ..application.run_service import load_run_result, owner_state_dir
    from ..evidence.ledger import EvidenceLedger

    load_run_result(state_dir, run_id, owner=owner)
    ledger = EvidenceLedger.load(run_id, owner_state_dir(state_dir, owner))
    return [{"ledger_id": item.ledger_id, "url": item.final_url or item.url,
             "source_tool": item.source_tool, "retrieved_at": item.retrieved_at,
             "snippet": item.snippet[:2000], "snippet_truncated": len(item.snippet) > 2000,
             "confidence": item.confidence}
            for item in ledger.items() if item.ledger_id in ledger_ids]
