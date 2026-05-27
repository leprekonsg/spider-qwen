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
from ..modes.classifier import ModeClassifier


def _state_dir() -> str:
    return os.getenv("SPIDER_QWEN_STATE_DIR", ".spider_qwen")


def _build_controller(args: argparse.Namespace) -> Controller:
    search_provider = fetch_provider = None
    if getattr(args, "offline", False):
        from ..tools.search_service import MockSearchProvider
        from ..tools.fetch_service import MockFetchProvider

        search_provider = MockSearchProvider()
        fetch_provider = MockFetchProvider()
    return Controller(
        search_provider=search_provider,
        fetch_provider=fetch_provider,
        state_dir=_state_dir(),
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
    if args.evidence_command != "show":
        print("usage: spider-qwen evidence show <run_id>", file=sys.stderr)
        return 2
    ledger = EvidenceLedger.load(args.run_id, _state_dir())
    if len(ledger) == 0:
        print(f"No evidence found for run '{args.run_id}' under {_state_dir()}", file=sys.stderr)
        return 1
    print(json.dumps([item.model_dump() for item in ledger.items()], indent=2))
    return 0


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
    p_run.set_defaults(func=_cmd_run)

    p_ev = sub.add_parser("evidence", help="Inspect the evidence ledger of a run")
    p_ev.add_argument("evidence_command", choices=["show"])
    p_ev.add_argument("run_id")
    p_ev.set_defaults(func=_cmd_evidence)

    p_bench = sub.add_parser("benchmark", help="Run the gold-set benchmark")
    p_bench.add_argument("--gold-set", required=True)
    p_bench.add_argument("--live", action="store_true", help="Use live providers instead of mock")
    p_bench.set_defaults(func=_cmd_benchmark)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
