"""spider-qwen command-line interface.

  spider-qwen classify "office cleaning Singapore"
  spider-qwen run "office cleaning Singapore" --mode auto
  spider-qwen run "500 ergonomic chairs Singapore" --mode product_exact_price
  spider-qwen evidence show <run_id>
  spider-qwen benchmark --gold-set spider_qwen/benchmarks/gold_set.json

Use --offline to run with deterministic mock providers (no API keys needed).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from ..agent.controller import Controller
from ..evidence.ledger import EvidenceLedger
from ..evidence.models import EvidenceRef, utc_now_iso
from ..evidence.verifier import verify_ledger
from ..evidence.graph import render_supplier_graph
from ..governance.review_events import ReviewStatusTransitionError, ReviewStore
from ..memory.decay import apply_decay, is_stale
from ..memory.revalidation import Revalidator
from ..memory.semantic import SemanticMemory
from ..modes.classifier import ModeClassifier


def _state_dir() -> str:
    return os.getenv("SPIDER_QWEN_STATE_DIR", ".spider_qwen")


def _build_controller(args: argparse.Namespace) -> Controller:
    search_provider = fetch_provider = None
    qwen_json_extractor = None
    if getattr(args, "offline", False):
        from ..tools.search_service import MockSearchProvider
        from ..tools.fetch_service import MockFetchProvider

        search_provider = MockSearchProvider()
        fetch_provider = MockFetchProvider()
        if getattr(args, "qwen_json", False) or _env_true("QWEN_STRUCTURED_EXTRACTION_ENABLED"):
            from ..tools.qwen_json_extractor import MockQwenJsonExtractor

            qwen_json_extractor = MockQwenJsonExtractor()
    elif getattr(args, "qwen_json", False):
        from ..tools.qwen_json_extractor import QwenJsonExtractor

        qwen_json_extractor = QwenJsonExtractor()
    return Controller(
        search_provider=search_provider,
        fetch_provider=fetch_provider,
        qwen_json_extractor=qwen_json_extractor,
        state_dir=_state_dir(),
        require_review=getattr(args, "require_review", None),
    )


def _cmd_classify(args: argparse.Namespace) -> int:
    result = ModeClassifier().classify(args.query)
    print(json.dumps(result.model_dump(), indent=2))
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    controller = _build_controller(args)
    result = asyncio.run(controller.run(args.query, mode=args.mode, target_country=args.country))
    print(json.dumps(result.model_dump(mode="json"), indent=2))
    return 0


def _cmd_evidence(args: argparse.Namespace) -> int:
    if args.evidence_command not in {"show", "verify", "graph"}:
        print("usage: spider-qwen evidence [show|verify|graph] <run_id>", file=sys.stderr)
        return 2
    try:
        ledger = EvidenceLedger.load(args.run_id, _state_dir())
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if len(ledger) == 0:
        print(f"No evidence found for run '{args.run_id}' under {_state_dir()}", file=sys.stderr)
        return 1
    if args.evidence_command == "verify":
        result = verify_ledger(ledger)
        print(json.dumps(result.model_dump(), indent=2))
        return 0 if result.ok else 1
    if args.evidence_command == "graph":
        print(render_supplier_graph(ledger))
        return 0
    print(json.dumps([item.model_dump() for item in ledger.items()], indent=2))
    return 0


def _cmd_memory(args: argparse.Namespace) -> int:
    memory = SemanticMemory(_state_dir())
    if args.memory_command == "show":
        memory.maintain()
        rows = []
        for fact in memory.all():
            rows.append(
                {
                    **fact.model_dump(mode="json"),
                    "decayed_confidence": round(apply_decay(fact), 4),
                    "is_stale": is_stale(fact),
                    "ttl_status": fact.status,
                }
            )
        print(json.dumps(rows, indent=2))
        return 0
    if args.memory_command == "revalidate":
        if not args.fact_id:
            print("usage: spider-qwen memory revalidate <fact_id>", file=sys.stderr)
            return 2
        fact = memory.get(args.fact_id)
        if fact is None:
            print(f"No semantic fact found for '{args.fact_id}'", file=sys.stderr)
            return 1
        refs = fact.evidence_refs
        if args.ledger_id and args.url and args.snippet_hash:
            refs = [
                EvidenceRef(
                    ledger_id=args.ledger_id,
                    url=args.url,
                    snippet_hash=args.snippet_hash,
                    retrieved_at=args.retrieved_at or utc_now_iso(),
                )
            ]
        refreshed = Revalidator(memory).revalidate(
            fact_id=args.fact_id,
            observed_value=args.value,
            evidence_refs=refs,
            new_confidence=args.confidence,
        )
        print(json.dumps(refreshed.model_dump(mode="json") if refreshed else None, indent=2))
        return 0
    print("usage: spider-qwen memory [show|revalidate]", file=sys.stderr)
    return 2


def _cmd_review(args: argparse.Namespace) -> int:
    store = ReviewStore(_state_dir())
    if args.review_command == "list":
        status = args.status if args.status != "all" else None
        print(json.dumps([e.model_dump() for e in store.list(status=status)], indent=2))
        return 0
    if args.review_command in {"approve", "reject"}:
        if not args.event_id:
            print(f"usage: spider-qwen review {args.review_command} <event_id>", file=sys.stderr)
            return 2
        try:
            event = store.approve(args.event_id) if args.review_command == "approve" else store.reject(args.event_id)
        except ReviewStatusTransitionError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if event is None:
            print(f"No review event found for '{args.event_id}'", file=sys.stderr)
            return 1
        # Approval releases the withheld RFQ draft carried in detail.rfq_draft.
        print(json.dumps(event.model_dump(), indent=2))
        return 0
    print("usage: spider-qwen review [list|approve|reject]", file=sys.stderr)
    return 2


def _cmd_benchmark(args: argparse.Namespace) -> int:
    from ..benchmarks.evaluate_service_mode import run_gold_set

    summary = run_gold_set(args.gold_set, offline=not args.live)
    print(json.dumps(summary, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="spider-qwen", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_classify = sub.add_parser("classify", help="Classify a query into a procurement mode")
    p_classify.add_argument("query")
    p_classify.set_defaults(func=_cmd_classify)

    p_run = sub.add_parser("run", help="Run the procurement research pipeline")
    p_run.add_argument("query")
    p_run.add_argument(
        "--mode", default="auto",
        choices=["auto", "product_exact_price", "service_quote_required", "contact_enrichment_only", "revalidation"],
    )
    p_run.add_argument("--country", default=None, help="Target country (e.g. Singapore)")
    p_run.add_argument("--offline", action="store_true", help="Use deterministic mock providers")
    p_run.add_argument("--qwen-json", action="store_true", help="Enable mocked Qwen JSON extraction when used with --offline")
    p_run.add_argument("--require-review", action="store_true", default=None, help="Persist HITL review gates for this run")
    p_run.set_defaults(func=_cmd_run)

    p_ev = sub.add_parser("evidence", help="Inspect the evidence ledger of a run")
    p_ev.add_argument("evidence_command", choices=["show", "verify", "graph"])
    p_ev.add_argument("run_id")
    p_ev.set_defaults(func=_cmd_evidence)

    p_mem = sub.add_parser("memory", help="Inspect or revalidate semantic memory")
    p_mem.add_argument("memory_command", choices=["show", "revalidate"])
    p_mem.add_argument("fact_id", nargs="?")
    p_mem.add_argument("--value", default=None)
    p_mem.add_argument("--confidence", type=float, default=0.85)
    p_mem.add_argument("--ledger-id", default=None)
    p_mem.add_argument("--url", default=None)
    p_mem.add_argument("--snippet-hash", default=None)
    p_mem.add_argument("--retrieved-at", default=None)
    p_mem.set_defaults(func=_cmd_memory)

    p_review = sub.add_parser("review", help="Inspect or approve human review events")
    p_review.add_argument("review_command", choices=["list", "approve", "reject"])
    p_review.add_argument("event_id", nargs="?")
    p_review.add_argument("--status", choices=["all", "pending", "approved", "rejected"], default="pending")
    p_review.set_defaults(func=_cmd_review)

    p_bench = sub.add_parser("benchmark", help="Run the gold-set benchmark")
    p_bench.add_argument("--gold-set", required=True)
    p_bench.add_argument("--live", action="store_true", help="Use live providers instead of mock")
    p_bench.set_defaults(func=_cmd_benchmark)
    return parser


def _env_true(name: str) -> bool:
    return os.getenv(name, "").lower() in {"1", "true", "yes", "on"}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
