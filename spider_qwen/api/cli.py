"""spider-qwen command-line interface.

  spider-qwen classify "office cleaning Singapore"
  spider-qwen run "office cleaning Singapore" --mode auto
  spider-qwen run "500 ergonomic chairs Singapore" --mode product_exact_price
  spider-qwen run "NE5532 substitute" --reason     # multi-trajectory reasoning spine
  spider-qwen evidence show <run_id>
  spider-qwen evidence prove <run_id>   # RFC 6962 citation proof + tamper demo
  spider-qwen benchmark --gold-set spider_qwen/benchmarks/gold_set.json

Use --offline to run with deterministic mock providers (no API keys needed).
Use --reason to explore several strategy trajectories with bounded recursive
repair and pick the winner via the deterministic Process Reward Model (returns a
ReasoningResult with a why-it-won explanation instead of the standard RunResult).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from ..agent.controller import Controller
from ..evidence.ledger import EvidenceLedger, sth_signing_key_from_env
from ..evidence.models import EvidenceRef, utc_now_iso
from ..evidence.verifier import verify_ledger
from ..evidence.graph import render_supplier_graph
from ..governance.review_events import ReviewStatusTransitionError, ReviewStore
from ..memory.decay import apply_decay, is_stale, memory_stability_days
from ..memory.episodic import EpisodicMemory
from ..memory.notes import NoteStore
from ..memory.reflections import ReflectionEngine
from ..memory.revalidation import Revalidator
from ..memory.semantic import SemanticMemory
from ..modes.classifier import ModeClassifier


def _state_dir() -> str:
    return os.getenv("SPIDER_QWEN_STATE_DIR", ".spider_qwen")


def _build_controller(args: argparse.Namespace) -> Controller:
    # offline=True is the controller-level guarantee: mock search/fetch AND no
    # live Qwen client (router, NLI, extractor), even when --judged-demo
    # enables their flags and an API key is in the env. Only the --qwen-json
    # arg (no env flag) needs explicit wiring here.
    offline = getattr(args, "offline", False)
    qwen_json_extractor = None
    if getattr(args, "qwen_json", False):
        if offline:
            from ..tools.qwen_json_extractor import MockQwenJsonExtractor

            qwen_json_extractor = MockQwenJsonExtractor()
        else:
            from ..tools.qwen_json_extractor import QwenJsonExtractor

            qwen_json_extractor = QwenJsonExtractor()
    return Controller(
        qwen_json_extractor=qwen_json_extractor,
        state_dir=_state_dir(),
        verify=True if getattr(args, "judged_demo", False) else None,
        require_review=getattr(args, "require_review", None),
        offline=offline,
    )


def _cmd_classify(args: argparse.Namespace) -> int:
    result = ModeClassifier().classify(args.query)
    print(json.dumps(result.model_dump(), indent=2))
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    try:
        # Fail a malformed STH signing key now, not at end-of-run persist.
        sth_signing_key_from_env()
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    prior_env = None
    if getattr(args, "judged_demo", False):
        prior_env = _apply_judged_demo_profile(args)
    # The profile env stays in place for the WHOLE run, not just controller
    # construction: a policy flag read lazily mid-run must see the same values.
    try:
        controller = _build_controller(args)
        if getattr(args, "reason", False):
            # Opt-in multi-trajectory reasoning spine; emits a ReasoningResult.
            result = asyncio.run(controller.run_reasoning(args.query, mode=args.mode, target_country=args.country))
        else:
            result = asyncio.run(controller.run(
                args.query, mode=args.mode, target_country=args.country,
                high_risk=getattr(args, "high_risk", False),
                serendipity=getattr(args, "serendipity", False),
            ))
    finally:
        if prior_env is not None:
            _restore_env(prior_env)
    print(json.dumps(result.model_dump(mode="json"), indent=2))
    return 0


def _cmd_evidence(args: argparse.Namespace) -> int:
    if args.evidence_command not in {"show", "verify", "graph", "prove"}:
        print("usage: spider-qwen evidence [show|verify|graph|prove] <run_id>", file=sys.stderr)
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
        chain = ledger.verify_chain()  # T-2.4: re-walk the Merkle hash chain
        out = result.model_dump()
        out["chain_checked"] = chain.checked
        out["chain_ok"] = chain.ok
        out["chain_issues"] = [i.model_dump() for i in chain.issues]
        print(json.dumps(out, indent=2))
        return 0 if (result.ok and chain.ok) else 1
    if args.evidence_command == "graph":
        print(render_supplier_graph(ledger))
        return 0
    if args.evidence_command == "prove":
        return _evidence_prove(ledger, getattr(args, "ledger_id", None))
    print(json.dumps([item.model_dump() for item in ledger.items()], indent=2))
    return 0


def _evidence_prove(ledger: EvidenceLedger, ledger_id: str | None) -> int:
    """RFC 6962 citation proof for one ledger row, plus a tamper demonstration.

    Emits the inclusion proof an external party can verify without the ledger,
    verifies it, then flips one hex digit of the leaf and shows the same proof
    fail -- the transparency log made visible. With the crypto extra installed
    the tree head is also Ed25519-signed and checked against the right and a
    wrong trust anchor.
    """
    from ..evidence.transparency import MerkleLog, verify_citation

    items = ledger.items()
    target = items[0] if ledger_id is None else next(
        (i for i in items if i.ledger_id == ledger_id), None
    )
    if target is None:
        print(f"ledger_id '{ledger_id}' not found in run '{ledger.run_id}'. "
              f"Use 'spider-qwen evidence show {ledger.run_id}' to list rows.", file=sys.stderr)
        return 2

    log = MerkleLog.from_ledger(ledger)
    proof = log.citation_proof(ledger, target.ledger_id)
    ok = verify_citation(proof)

    # Tamper beat: one flipped hex digit in the committed leaf must break the proof.
    flipped = ("0" if proof.leaf_data[0] != "0" else "1") + proof.leaf_data[1:]
    tampered_ok = verify_citation(proof.model_copy(update={"leaf_data": flipped}))

    out: dict = {
        "run_id": ledger.run_id,
        "ledger_id": target.ledger_id,
        "url": target.url,
        "citation_proof": proof.model_dump(mode="json"),
        "proof_verified": ok,
        "tamper_demo": {
            "description": "same proof with one hex digit of the leaf flipped",
            "proof_verified": tampered_ok,
        },
    }
    try:
        from ..evidence.transparency import (
            generate_signing_key,
            sign_tree_head,
            verify_signed_tree_head,
        )

        # Demo keypair (ephemeral). In production the verifier pins the public
        # key out of band; verifying against the STH's embedded key would let
        # an attacker re-sign a rewritten ledger and self-validate.
        sth = sign_tree_head(proof.tree_head, generate_signing_key())
        out["signed_tree_head"] = {
            "sth": sth.model_dump(mode="json"),
            "verified_against_trust_anchor": verify_signed_tree_head(sth, sth.public_key),
            "verified_against_attacker_key": verify_signed_tree_head(sth, "00" * 32),
        }
    except ImportError as exc:
        out["signed_tree_head"] = {"unavailable": str(exc)}

    print(json.dumps(out, indent=2))
    return 0 if ok and not tampered_ok else 1


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
                    "stability_days": round(memory_stability_days(fact), 2),
                    "is_stale": is_stale(fact),
                    "ttl_status": fact.status,
                }
            )
        print(json.dumps(rows, indent=2))
        return 0
    if args.memory_command == "reflect":
        reflections = ReflectionEngine().reflect(
            memory.all(), EpisodicMemory(_state_dir()).all()
        )
        print(json.dumps([r.model_dump(mode="json") for r in reflections], indent=2))
        return 0
    if args.memory_command == "notes":
        # Read-only view: build Zettelkasten notes from the current facts.
        store = NoteStore(state_dir=None)
        notes = [store.add_from_fact(f) for f in memory.all()]
        print(json.dumps([n.model_dump(mode="json") for n in notes], indent=2))
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
    print("usage: spider-qwen memory [show|revalidate|reflect|notes]", file=sys.stderr)
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


def _cmd_skills(args: argparse.Namespace) -> int:
    from ..skills.registry import SkillRegistry

    reg = SkillRegistry.load()
    if args.skills_command == "list":
        print(json.dumps([s.model_dump(mode="json") for s in reg.all()], indent=2))
        return 0
    if args.skills_command == "match":
        if not args.query:
            print('usage: spider-qwen skills match "<query>"', file=sys.stderr)
            return 2
        matches = reg.match(args.query)
        print(json.dumps(
            [{"name": m.skill.name, "score": m.score, "description": m.skill.description} for m in matches],
            indent=2,
        ))
        return 0
    if args.skills_command == "show":
        if not args.query:
            print("usage: spider-qwen skills show <name>", file=sys.stderr)
            return 2
        skill = reg.get(args.query)
        if skill is None:
            print(f"No skill named '{args.query}' under .qwen/skills", file=sys.stderr)
            return 1
        print(json.dumps(skill.model_dump(mode="json"), indent=2))
        return 0
    print("usage: spider-qwen skills [list|match|show] [query]", file=sys.stderr)
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
        choices=["auto", "product_exact_price", "service_quote_required", "contact_enrichment_only",
                 "revalidation", "electronics_substitution"],
    )
    p_run.add_argument("--country", default=None, help="Target country (e.g. Singapore)")
    p_run.add_argument("--offline", action="store_true", help="Use deterministic mock providers")
    p_run.add_argument("--reason", action="store_true",
                       help="Use the multi-trajectory reasoning spine (PPRM winner selection); emits a ReasoningResult")
    p_run.add_argument("--high-risk", action="store_true", default=False,
                       help="Tag the run high_risk_procurement: the cost router forces max for the decision step")
    p_run.add_argument("--serendipity", action="store_true", default=False,
                       help="Discovery sidecar: populate S1/S2/S3 from real components (graph/Wayback/signals/DMSMS); default run is unchanged")
    p_run.add_argument("--qwen-json", action="store_true", help="Enable mocked Qwen JSON extraction when used with --offline")
    p_run.add_argument("--judged-demo", action="store_true", default=False,
                       help="Enable the opt-in judged-demo profile: Qwen extraction, verification/trust surfaces, and S1/S2/S3 sidecar")
    p_run.add_argument("--require-review", action="store_true", default=None, help="Persist HITL review gates for this run")
    p_run.set_defaults(func=_cmd_run)

    p_ev = sub.add_parser("evidence", help="Inspect the evidence ledger of a run")
    p_ev.add_argument("evidence_command", choices=["show", "verify", "graph", "prove"])
    p_ev.add_argument("run_id")
    p_ev.add_argument("--ledger-id", default=None,
                      help="prove: which row to prove (default: first row)")
    p_ev.set_defaults(func=_cmd_evidence)

    p_mem = sub.add_parser("memory", help="Inspect or revalidate semantic memory")
    p_mem.add_argument("memory_command", choices=["show", "revalidate", "reflect", "notes"])
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

    p_skills = sub.add_parser("skills", help="List, match, or show project Qwen Agent Skills")
    p_skills.add_argument("skills_command", choices=["list", "match", "show"])
    p_skills.add_argument("query", nargs="?", help="Query for 'match' or skill name for 'show'")
    p_skills.set_defaults(func=_cmd_skills)

    p_bench = sub.add_parser("benchmark", help="Run the gold-set benchmark")
    p_bench.add_argument("--gold-set", required=True)
    p_bench.add_argument("--live", action="store_true", help="Use live providers instead of mock")
    p_bench.set_defaults(func=_cmd_benchmark)
    return parser


def _env_true(name: str) -> bool:
    return os.getenv(name, "").lower() in {"1", "true", "yes", "on"}


def _apply_judged_demo_profile(args: argparse.Namespace) -> dict[str, str | None]:
    """Opt into demo-facing Qwen/trust features without changing v1 defaults."""
    args.qwen_json = True
    args.serendipity = True
    names = {
        "QWEN_STRUCTURED_EXTRACTION_ENABLED": "1",
        "QWEN_ROUTER_FALLBACK_ENABLED": "1",
        "QWEN_PAGE_JUDGE_ENABLED": "1",
        "SPIDER_QWEN_VERIFICATION_ENABLED": "1",
        "QWEN_NLI_ENABLED": "1",
    }
    prior = {name: os.environ.get(name) for name in names}
    for name, value in names.items():
        os.environ.setdefault(name, value)
        # setdefault: an explicitly exported value wins over the profile. Say
        # so -- a judged demo silently degraded to regex is worse than noise.
        if not _env_true(name):
            print(
                f"judged-demo: {name}={os.environ[name]!r} from the environment "
                "overrides the profile; this surface stays disabled.",
                file=sys.stderr,
            )
    return prior


def _restore_env(prior: dict[str, str | None]) -> None:
    for name, value in prior.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
