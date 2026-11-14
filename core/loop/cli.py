#!/usr/bin/env python3
"""Shim for the retired loop CLI (X1 D1). The engine is core/cli/search.py.

Stamped projects keep running ``python3 harness/core/loop/cli.py`` (seed/Makefile ``check`` and
``loop`` targets). This file maps the legacy subcommands onto the new command:

  cli.py run [--max-iterations N] [--mock-workers]   -> search.py run   (--mock for --mock-workers)
  cli.py status                                      -> search.py status
  cli.py validate                                    -> search.py validate

``--root``, ``--project`` and the oracle come from the project's adapter.yaml, and the backstops
(max_iterations, cost_budget_usd, wall_clock_minutes), escalation and graduate target become the
search budget flags. The loop-disabled path keeps the legacy exit codes (run: 2, validate: 0).

Removed subcommands (``once``, ``verify-flags``) exit 2 and name the command that replaces them.
Without ``--mock-workers``, ``run`` reaches search.py without ``--mock``, which refuses (exit 1):
no model port is wired into the search yet.

LangGraph is imported only when a search command runs, so a disabled loop never needs it.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import yaml  # noqa: E402

from core.kernel import paths  # noqa: E402

SEARCH = "core/cli/search.py"
EXIT_USAGE = 2
REMOVED = {
    "once": "run --max-iterations 1",
    "verify-flags": "validate",
}


def _root(args) -> Path:
    return Path(args.root).resolve() if args.root else paths.find_root()


def _adapter(root: Path) -> dict:
    path = root / paths.ADAPTER_REL
    if not path.is_file():
        raise SystemExit(f"loop: no adapter at {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    loop = dict(raw.get("loop") or {})
    loop.setdefault("escalation", dict(raw.get("escalation") or {}))
    loop["_project"] = str(raw.get("project") or "")
    return loop


def _problems(loop: dict) -> "list[str]":
    """The legacy refuse-if-unbounded rule and the enabled-loop sanity checks."""
    problems: "list[str]" = []
    bk = loop.get("backstops") or {}
    bounds = (bk.get("max_iterations"), bk.get("cost_budget_usd"), bk.get("wall_clock_minutes"))
    if all(b is None for b in bounds) and not loop.get("i_understand_unbounded"):
        problems.append("all three backstops (max_iterations, cost_budget_usd, "
                        "wall_clock_minutes) are null, set at least one, or set "
                        "i_understand_unbounded: true")
    if loop.get("enabled"):
        if not str(loop.get("oracle") or "").strip():
            problems.append("loop.enabled is true but loop.oracle is empty")
        sc = loop.get("self_consistency") or {}
        if sc.get("enabled") and int(sc.get("n") or 0) < 1:
            problems.append("self_consistency.n must be >= 1 when enabled")
    return problems


def _search_argv(verb: str, root: Path, loop: dict, args) -> list:
    bk = loop.get("backstops") or {}
    argv = [verb, "--root", str(root), "--project", loop["_project"],
            "--oracle", str(loop["oracle"])]
    max_iter = getattr(args, "max_iterations", None)
    if max_iter is None:
        max_iter = bk.get("max_iterations")
    if max_iter is not None:
        argv += ["--max-iterations", str(max_iter)]
    if bk.get("cost_budget_usd") is not None:
        argv += ["--max-usd", str(bk["cost_budget_usd"])]
    if bk.get("wall_clock_minutes") is not None:
        argv += ["--max-wall-s", str(float(bk["wall_clock_minutes"]) * 60)]
    if loop.get("i_understand_unbounded"):
        argv.append("--allow-unbounded")
    fail_streak = (loop.get("escalation") or {}).get("fail_streak_to_escalate")
    if fail_streak is not None:
        argv += ["--fail-streak-to-escalate", str(fail_streak)]
    grad = (loop.get("stop") or {}).get("graduate_target")
    if grad is not None:
        argv += ["--graduate-target", str(grad)]
    sc = loop.get("self_consistency") or {}
    if sc.get("enabled"):
        argv += ["--self-consistency", str(int(sc.get("n") or 1))]
    if verb == "run" and getattr(args, "mock_workers", False):
        argv.append("--mock")
    return argv


def _dispatch(argv: list) -> int:
    try:
        from core.cli import search  # imports LangGraph; only search commands reach here
    except ImportError as exc:
        print(f"loop: the search engine needs LangGraph ({exc}); run this command with a "
              f"Python that has it installed (the project venv)", file=sys.stderr)
        return 1
    return search.main(argv)


def cmd_run(args) -> int:
    root = _root(args)
    loop = _adapter(root)
    problems = _problems(loop)
    if problems:
        for p in problems:
            print(f"loop: REFUSING to start: {p}", file=sys.stderr)
        return EXIT_USAGE
    if not loop.get("enabled"):
        print("loop: loop.enabled is false, flip it in harness/adapter/adapter.yaml "
              "and implement harness/adapter/oracle.py first", file=sys.stderr)
        return EXIT_USAGE
    return _dispatch(_search_argv("run", root, loop, args))


def cmd_status(args) -> int:
    root = _root(args)
    loop = _adapter(root)
    return _dispatch(["status", "--root", str(root), "--project", loop["_project"],
                      "--oracle", str(loop.get("oracle") or "core.kernel.toy_oracle:ToyOracle")])


def cmd_validate(args) -> int:
    root = _root(args)
    loop = _adapter(root)
    if not loop.get("enabled"):
        print("loop: disabled, config not gating (flip loop.enabled to activate Layer 2)")
        return 0
    problems = _problems(loop)
    if problems:
        for p in problems:
            print(f"loop config INVALID: {p}", file=sys.stderr)
        return EXIT_USAGE
    return _dispatch(_search_argv("validate", root, loop, args))


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=None, help="project root (default: discovered)")
    common.add_argument("--model", default=None, help="accepted for compatibility; not used")
    common.add_argument("--state-root", default=None,
                        help="removed: state lives in .gategraph/search/<project>/")

    p = argparse.ArgumentParser(prog="loop", description="GateGraph loop (shim for search.py)")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", parents=[common], help="run the search (shim)")
    run.add_argument("--max-iterations", type=int, default=None)
    run.add_argument("--mock-workers", action="store_true",
                     help="drive the search with the scripted stand-in model (no spend)")
    run.set_defaults(func=cmd_run)

    status = sub.add_parser("status", parents=[common], help="print search status (shim)")
    status.set_defaults(func=cmd_status)

    val = sub.add_parser("validate", parents=[common], help="search config sanity (shim)")
    val.set_defaults(func=cmd_validate)
    return p


def main(argv: "list[str] | None" = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in REMOVED:
        name = argv[0]
        print(f"loop {name}: removed with the legacy loop engine (X1). "
              f"Use: python3 {SEARCH} {REMOVED[name]} --root R --project P "
              f"--oracle MOD:CLS [--mock]", file=sys.stderr)
        return EXIT_USAGE
    args = build_parser().parse_args(argv)
    if args.state_root:
        print("loop: --state-root is removed; state lives in .gategraph/search/<project>/",
              file=sys.stderr)
        return EXIT_USAGE
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
