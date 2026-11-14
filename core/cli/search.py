"""The variant-search command: ``run``, ``status`` and ``validate`` (plan section 1.14; brief S2).

    python core/cli/search.py run      --root R --project P --oracle MOD:CLS [budget flags] [--mock]
    python core/cli/search.py status   --root R --project P
    python core/cli/search.py validate --root R --project P --oracle MOD:CLS [budget flags]

``run`` continues the project's search thread from its last checkpoint, or starts it. When the
run waits on an escalation, ``--answer retry|stop`` answers it. Without an answer it prints the
waiting question and exits 3.

``--mock`` is the only model this command can drive in S2: a scripted stand-in that proposes cells
from the oracle's proposal space and always judges clean. No network and no real model. Without
it, ``run`` refuses, because no model port is wired into the graph yet.

Exit codes: 0 done (complete, abandoned or escalated), 1 refused or invalid, 3 waiting on an
escalation, 2 usage.

Standard library, LangGraph, core.graph and core.kernel only. No product name appears here.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core.agent.models import ModelResult  # noqa: E402
from core.graph import runner, store  # noqa: E402
from core.graph.search import (JUDGEMENT_SCHEMA, PROPOSAL_SCHEMA, REGISTRY_NAME,  # noqa: E402
                               build_search_graph, initial_state, thread_id)
from core.kernel import paths  # noqa: E402
from core.kernel.budget import UnboundedBudget, refuse_unbounded  # noqa: E402
from core.kernel.locks import LockHeld  # noqa: E402
from core.kernel.oracle import OracleLoadError, load_oracle  # noqa: E402
from core.kernel.registry import Registry  # noqa: E402
from core.kernel.types import Budget, Spend  # noqa: E402

DEFAULT_ORACLE = "core.kernel.toy_oracle:ToyOracle"
EXIT_DONE, EXIT_REFUSED, EXIT_WAITING = 0, 1, 3


class MockModel:
    """Scripted stand-in for the model: cycles the proposal space and always judges clean.

    It answers by schema name, so it serves both the proposal and the judgement calls. It
    spends nothing and never touches the network. The judgement matches the legacy
    ``--mock-workers`` checker: every soft pass the oracle declares holds, gaming is clean, the
    mechanism holds, the variant is promising and not exhausted.
    """

    def __init__(self, space, soft_passes=()):
        self.space = list(space)
        self.soft_passes = list(soft_passes)
        self.count = 0

    def complete(self, prompt, schema=None):
        self.count += 1
        name = (schema or {}).get("name")
        if name == PROPOSAL_SCHEMA["name"]:
            cell = self.space[self.count % len(self.space)] if self.space \
                else f"mock-{self.count}"
            data = {"cell": cell, "hypothesis": "mock hypothesis",
                    "mechanism": f"mock mechanism {self.count}", "kill_criteria": "mock",
                    "source": "OK = True\n"}
        elif name == JUDGEMENT_SCHEMA["name"]:
            data = {"soft_passes": {name: True for name in self.soft_passes},
                    "gaming_clean": True,
                    "mechanism_sentence": "mock mechanism holds in the claimed regime",
                    "structurally_promising": True, "exhausted": False, "notes": "mock"}
        else:
            raise ValueError(f"unknown schema {name!r}")
        return ModelResult(text="", data=data, spend=Spend(model_calls=1))


def _budget(args) -> Budget:
    return Budget(max_usd=args.max_usd, max_iterations=args.max_iterations,
                  max_wall_s=args.max_wall_s, max_model_calls=args.max_model_calls,
                  allow_unbounded=args.allow_unbounded)


def _state_dir(root: Path, project: str) -> Path:
    return paths.state_dir(root) / "search" / project


def _build(args, *, mock: bool):
    """Build the graph for ``run`` and ``validate``. Refuses an unbounded budget."""
    oracle = load_oracle(args.oracle, args.root)
    budget = _budget(args)
    refuse_unbounded(budget)
    state_dir = _state_dir(Path(args.root), args.project)
    model = MockModel(oracle.proposal_space(), oracle.soft_passes()) if mock else None
    cfg = {"project": args.project, "state_dir": state_dir, "oracle": oracle, "model": model,
           "budget": budget, "slots": args.slots, "max_repairs": args.max_repairs,
           "max_reproposals": args.max_reproposals, "max_revisions": args.max_revisions,
           "self_consistency": args.self_consistency,
           "fail_streak_to_escalate": args.fail_streak_to_escalate,
           "graduate_target": args.graduate_target,
           "checkpointer": store.make_checkpointer({"root": args.root})}
    return build_search_graph(cfg), state_dir


def _add_budget(parser) -> None:
    parser.add_argument("--oracle", default=DEFAULT_ORACLE, help="module.path:ClassName")
    parser.add_argument("--max-usd", type=float, default=None)
    parser.add_argument("--max-iterations", type=int, default=None)
    parser.add_argument("--max-wall-s", type=float, default=None)
    parser.add_argument("--max-model-calls", type=int, default=None)
    parser.add_argument("--allow-unbounded", action="store_true",
                        help="allow a run with no backstop (refused otherwise)")
    parser.add_argument("--slots", type=int, default=1)
    parser.add_argument("--max-repairs", type=int, default=2)
    parser.add_argument("--max-reproposals", type=int, default=1)
    parser.add_argument("--max-revisions", type=int, default=0)
    parser.add_argument("--self-consistency", type=int, default=1)
    parser.add_argument("--fail-streak-to-escalate", type=int, default=None)
    parser.add_argument("--graduate-target", type=int, default=None)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="search", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "status", "validate"):
        p = sub.add_parser(name)
        p.add_argument("--root", required=True)
        p.add_argument("--project", required=True)
        if name != "status":
            _add_budget(p)
        if name == "run":
            p.add_argument("--mock", action="store_true",
                           help="drive the search with the scripted stand-in model")
            p.add_argument("--answer", choices=("retry", "stop"), default=None,
                           help="answer a waiting escalation")
        if name == "status":
            p.add_argument("--oracle", default=DEFAULT_ORACLE)
    return parser


def cmd_validate(args) -> int:
    try:
        _build(args, mock=True)
    except UnboundedBudget as exc:
        print(f"validate: refused: {exc}")
        return EXIT_REFUSED
    except OracleLoadError as exc:
        print(f"validate: refused: {exc}")
        return EXIT_REFUSED
    print("validate: OK")
    return EXIT_DONE


def cmd_run(args) -> int:
    if not args.mock:
        print("run: refused: no model port is wired into the search in S2; use --mock")
        return EXIT_REFUSED
    try:
        graph, state_dir = _build(args, mock=True)
    except (UnboundedBudget, OracleLoadError) as exc:
        print(f"run: refused: {exc}")
        return EXIT_REFUSED
    thread = thread_id(args.project)
    waiting = runner.pending(graph, thread)
    if waiting and args.answer is None:
        print(f"run: waiting: escalation at fail streak {waiting[0]['fail_streak']}; "
              f"re-run with --answer retry or --answer stop")
        return EXIT_WAITING
    if waiting:
        from langgraph.types import Command
        new_input = Command(resume=args.answer)
    elif runner.checkpoint_reader(graph)(thread) is None:
        new_input = initial_state(args.project)  # a new search
    else:
        new_input = None                          # continue from the last checkpoint
    seen = []
    try:
        runner.run(graph, thread, new_input, seen.append,
                   lock_dir=paths.state_dir(args.root) / "locks")
    except LockHeld as exc:
        print(f"run: refused: {exc}")
        return EXIT_REFUSED
    snap = seen[-1]
    graduated = Registry(state_dir / REGISTRY_NAME).graduated_ids()
    print(f"run: status: {snap.values.get('status')}")
    print(f"stop_reason: {snap.values.get('stop_reason')}")
    print(f"graduated: {', '.join(graduated) or 'none'}")
    return EXIT_WAITING if runner.pending(graph, thread) else EXIT_DONE


def cmd_status(args) -> int:
    """Read-only: builds the graph with an open budget (no node runs) and reads the checkpoint."""
    oracle = load_oracle(args.oracle, args.root)
    state_dir = _state_dir(Path(args.root), args.project)
    graph = build_search_graph({
        "project": args.project, "state_dir": state_dir, "oracle": oracle, "model": None,
        "budget": Budget(allow_unbounded=True),
        "checkpointer": store.make_checkpointer({"root": args.root})})
    thread = thread_id(args.project)
    snap = runner.checkpoint_reader(graph)(thread)
    if snap is None:
        print("status: none (the project has no search yet)")
        return EXIT_DONE
    values = snap["values"]
    spend = values.get("spend") or {}
    graduated = Registry(state_dir / REGISTRY_NAME).graduated_ids()
    print(f"status: {values.get('status')}")
    print(f"stop_reason: {values.get('stop_reason')}")
    print(f"iterations: {spend.get('iterations', 0)}")
    print(f"fail_streak: {values.get('fail_streak', 0)}")
    print(f"spend_usd: {spend.get('usd', 0.0)}")
    print(f"graduated: {', '.join(graduated) or 'none'}")
    waiting = runner.pending(graph, thread)
    if waiting:
        print(f"waiting: {waiting[0].get('kind')} at fail streak {waiting[0].get('fail_streak')}")
    return EXIT_DONE


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    handlers = {"run": cmd_run, "status": cmd_status, "validate": cmd_validate}
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
