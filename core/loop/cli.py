#!/usr/bin/env python3
"""cli.py, the Layer 2 loop operator entrypoint (`make loop`).

  cli.py run [--max-iterations N] [--mock-workers] [--model M]   iterate (proceeds in-flight first)
  cli.py once                                                    a single iteration
  cli.py status                                                  print STATUS without running
  cli.py validate                                                loop-config sanity (make check hook)
  cli.py verify-flags [--live]                                   re-check the FLAGS.md claude -p spike

The loop refuses to start when disabled, when the oracle can't load, or when all three
backstops are null (refuse-if-unbounded, plan §8.4). ``--mock-workers`` swaps the real
``claude -p`` gateway for the scripted MockGateway so the whole machine can be exercised with
zero spend (pair with the shipped toy oracle: ``core.loop.toy_oracle:ToyOracle``).
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core import config as core_config  # noqa: E402
from core import paths as core_paths  # noqa: E402
from core.loop import manifest as loop_manifest  # noqa: E402
from core.loop import workers  # noqa: E402
from core.loop.backstops import validate_loop_config  # noqa: E402
from core.loop.brief import make_brief_fn  # noqa: E402
from core.loop.driver import Driver, compute_status  # noqa: E402
from core.loop.events import EventLog, render_status  # noqa: E402
from core.loop.gateway import ClaudeGateway, MockGateway  # noqa: E402
from core.loop.oracle import load_oracle  # noqa: E402
from core.loop.registry import Registry  # noqa: E402
from core.loop.statefile import StateStore  # noqa: E402

_OK, _WARN, _FAIL = "ok", "warn", "fail"
_ICON = {_OK: "✓", _WARN: "!", _FAIL: "✗"}


def _root(args) -> Path:
    return Path(args.root).resolve() if args.root else core_paths.find_repo_root()


def _state_root(root: Path, args) -> Path:
    return Path(args.state_root).resolve() if args.state_root \
        else root / "harness" / "state" / "loop"


def _loop_cfg(cfg) -> dict:
    """The dict the driver reads: the adapter's loop section + the top-level escalation tier
    (the §12 fail-streak / τ knobs live at adapter.yaml top level, not under loop:)."""
    loop = dict(cfg.loop or {})
    loop.setdefault("escalation", dict(cfg.escalation or {}))
    return loop


def build_driver(root: Path, loop_cfg: dict, args) -> Driver:
    oracle = load_oracle(loop_cfg["oracle"], root)
    gateway = MockGateway() if args.mock_workers else ClaudeGateway(model=args.model)
    state_root = _state_root(root, args)
    state_root.mkdir(parents=True, exist_ok=True)
    stamp = loop_manifest.compute_manifest(root).stamp()
    registry = Registry(state_root / "registry.jsonl")
    return Driver(
        oracle=oracle,
        gateway=gateway,
        registry=registry,
        store=StateStore(state_root),
        log=EventLog(state_root / "events.jsonl", stamp=stamp),
        cfg=loop_cfg,
        state_root=str(state_root),
        repo_root=root,
        brief_fn=make_brief_fn(registry),
    )


def cmd_run(args) -> int:
    root = _root(args)
    cfg = core_config.load(root)
    loop = _loop_cfg(cfg)
    problems = validate_loop_config(loop)
    if problems:
        for p in problems:
            print(f"loop: REFUSING to start: {p}", file=sys.stderr)
        return 2
    if not loop.get("enabled"):
        print("loop: loop.enabled is false, flip it in harness/adapter/adapter.yaml "
              "and implement harness/adapter/oracle.py first", file=sys.stderr)
        return 2
    drv = build_driver(root, loop, args)
    n = drv.run(max_iterations=args.max_iterations)
    print(f"loop: completed {n} iteration(s); graduated={drv.registry.graduated_ids()} "
          f"state={drv.state_root}")
    if drv._banner:
        print(f"loop: {drv._banner}")
    # Exit code must not lie: a fault-shaped stop (escalation, protected-file halt) is 1.
    # Backstop halts / graduate-target success / SIGINT / PAUSE are how healthy bounded
    # runs END, those stay 0.
    return 1 if drv.faulted else 0


def cmd_once(args) -> int:
    args.max_iterations = 1
    return cmd_run(args)


def cmd_status(args) -> int:
    root = _root(args)
    cfg = core_config.load(root)
    loop = _loop_cfg(cfg)
    state_root = _state_root(root, args)
    reg = Registry(state_root / "registry.jsonl")
    log = EventLog(state_root / "events.jsonl")
    latest = StateStore(state_root).load_latest()
    engine = compute_status(reg, log, loop)
    print(render_status(engine, latest.to_dict() if latest else None, log.tail(8)))
    return 0


def cmd_validate(args) -> int:
    root = _root(args)
    cfg = core_config.load(root)
    loop = _loop_cfg(cfg)
    if not loop.get("enabled"):
        print("loop: disabled, config not gating (flip loop.enabled to activate Layer 2)")
        return 0
    problems = validate_loop_config(loop)
    if problems:
        for p in problems:
            print(f"loop config INVALID: {p}", file=sys.stderr)
        return 2
    try:
        load_oracle(loop["oracle"], root)
    except Exception as exc:  # noqa: BLE001, any load failure is precisely what we're checking
        print(f"loop config INVALID: oracle does not load: {exc}", file=sys.stderr)
        return 2
    print("loop config OK (backstops bounded, oracle loads)")
    return 0


# ── verify-flags: re-run the FLAGS.md empirical spike (plan §8.6, risk §15.1) ──

def _flags_checks(root: Path, live: bool) -> "list[tuple[str, str, str]]":
    checks: "list[tuple[str, str, str]]" = []
    loop_dir = Path(__file__).resolve().parent

    # claude CLI present + version vs the FLAGS.md pin (version drift is Risk 1).
    installed = ""
    if shutil.which("claude"):
        try:
            out = subprocess.run(["claude", "--version"], capture_output=True, text=True,
                                 timeout=15)
            installed = (out.stdout or out.stderr).strip()
            checks.append((_OK, "claude CLI", installed or "installed"))
        except Exception as exc:  # noqa: BLE001
            checks.append((_WARN, "claude CLI", f"found on PATH but `--version` failed: {exc}"))
    else:
        checks.append((_FAIL, "claude CLI", "`claude` not on PATH, install Claude Code"))

    flags_text = (loop_dir / "FLAGS.md").read_text(encoding="utf-8")
    pin = re.search(r"CLI\s+(\d[\d.]*)", flags_text)
    pinned = pin.group(1) if pin else "?"
    if pinned != "?" and installed and pinned in installed:
        checks.append((_OK, "FLAGS.md version pin", f"installed CLI matches pinned {pinned}"))
    else:
        checks.append((_WARN, "FLAGS.md version pin",
                       f"FLAGS.md is pinned to CLI {pinned}, installed: {installed or 'unknown'} "
                       f"(semantics may have drifted; re-verify with --live)"))

    # subscription auth (workers scrub API keys, subscription-only by design).
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") \
            or (Path.home() / ".claude" / ".credentials.json").exists():
        checks.append((_OK, "subscription auth", "credentials present"))
    else:
        checks.append((_FAIL, "subscription auth",
                       "no CLAUDE_CODE_OAUTH_TOKEN and no ~/.claude/.credentials.json"))
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        checks.append((_WARN, "API-key isolation",
                       "ANTHROPIC_API_KEY/AUTH_TOKEN set, workers scrub it (subscription-only)"))
    else:
        checks.append((_OK, "API-key isolation", "no API key in env (subscription-only path)"))

    # write-scope guard: exercise the real hook's decision fn (FLAGS §5: the hook is PRIMARY).
    from core.hooks import write_guard
    scope = "/tmp/fh_scope_probe/cand"
    allow, _ = write_guard.decide(
        {"tool_name": "Write", "tool_input": {"file_path": scope + "/variant.py"}}, scope)
    deny_out, _ = write_guard.decide(
        {"tool_name": "Write", "tool_input": {"file_path": "/etc/passwd"}}, scope)
    deny_unset, _ = write_guard.decide(
        {"tool_name": "Write", "tool_input": {"file_path": scope + "/x"}}, None)
    if (allow, deny_out, deny_unset) == (0, 2, 2):
        checks.append((_OK, "write-scope guard",
                       "allows in-scope, denies out-of-scope + unset (fail-closed)"))
    else:
        checks.append((_FAIL, "write-scope guard",
                       f"misbehaving (in={allow}, out={deny_out}, unset={deny_unset}), want 0/2/2"))

    # methodology files the gateway inlines.
    missing = [f"agents/{n}.md" for n in ("maker", "checker", "judge")
               if not (loop_dir / "agents" / f"{n}.md").is_file()]
    checks.append((_FAIL, "agent prompts", "missing: " + ", ".join(missing)) if missing
                  else (_OK, "agent prompts", "maker/checker/judge present"))

    if live:
        # ONE cheap haiku probe covering the load-bearing FLAGS claims in a single call:
        # subscription auth works headless (§1), --json-schema conforms (§6), cost is
        # reported (§1), stdin redirect avoids the stall (§9).
        probe_schema = {"type": "object", "required": ["ok"], "additionalProperties": False,
                        "properties": {"ok": {"type": "boolean"}}}
        spec = workers.reasoner_spec(
            'Return exactly this JSON object and nothing else: {"ok": true}',
            model="haiku", budget_usd=0.10, timeout_s=180)
        try:
            data, res = workers.run_contract(spec, probe_schema)
            if data.get("ok") is True:
                checks.append((_OK, "live probe (haiku)",
                               f"envelope + json-schema conform; cost ${res.cost_usd:.4f}"))
            else:
                checks.append((_FAIL, "live probe (haiku)", f"unexpected payload: {data}"))
        except Exception as exc:  # noqa: BLE001, report, don't crash the preflight
            checks.append((_FAIL, "live probe (haiku)", f"{type(exc).__name__}: {exc}"))
    return checks


def cmd_verify_flags(args) -> int:
    root = _root(args)
    checks = _flags_checks(root, args.live)
    print("loop verify-flags: FLAGS.md empirical `claude -p` semantics\n")
    for status, name, detail in checks:
        print(f"  [{_ICON[status]}] {name:<22} {detail}")
    n_fail = sum(1 for s, _, _ in checks if s == _FAIL)
    print("\n" + ("READY: no hard failures." if n_fail == 0
                  else f"NOT READY: {n_fail} failing check(s) above."))
    return 2 if n_fail else 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=None, help="repo root (default: discovered)")
    common.add_argument("--state-root", default=None,
                        help="loop state dir (default: harness/state/loop)")
    common.add_argument("--mock-workers", action="store_true",
                        help="use the scripted MockGateway (no claude -p spend)")
    common.add_argument("--model", default="opus", help="worker model tier (default: opus)")

    p = argparse.ArgumentParser(prog="loop", description="Gatebench Layer 2 loop")
    sub = p.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", parents=[common], help="iterate to completion (proceeds first)")
    run.add_argument("--max-iterations", type=int, default=None)
    run.set_defaults(func=cmd_run)

    once = sub.add_parser("once", parents=[common], help="run a single iteration")
    once.set_defaults(func=cmd_once, max_iterations=1)

    status = sub.add_parser("status", parents=[common], help="print current status")
    status.set_defaults(func=cmd_status)

    val = sub.add_parser("validate", parents=[common],
                         help="loop-config sanity; exits 0 when the loop is disabled")
    val.set_defaults(func=cmd_validate)

    vf = sub.add_parser("verify-flags", parents=[common],
                        help="re-check the FLAGS.md claude -p spike (structural; --live spends)")
    vf.add_argument("--live", action="store_true",
                    help="add one real haiku probe (~$0.03) verifying the live semantics")
    vf.set_defaults(func=cmd_verify_flags)
    return p


def main(argv: "list[str] | None" = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
