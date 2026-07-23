#!/usr/bin/env python3
"""gate.py, lifecycle + evidence-chain manager for one unit at a time.

Subcommands:
  start    --unit <slug> [--title --session-id]     create unit + .active
  approve  <gate> --unit <slug> [--note --by]       set a human-approvable gate
  status   [--unit <slug>]                          show state / next command
  pause    --unit <slug>                            stop the Stop-gate blocking
  proceed   --unit <slug> [--session-id]             back to in_progress
  abandon  --unit <slug>                            mark abandoned, clear .active
  escalate --unit <slug> --note <why>               terminal "I'm stuck" state
  repair   --unit <slug>                            rebuild a corrupt manifest

gate.py is the ONLY writer of <unit_dir>/.active. Gates listed in
workflow.tool_set_gates refuse manual approve, they are settable only by the
tool that produced their evidence. Next-step text always comes from
adapter.yaml workflow.gate_commands (the single source of truth).
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import config, failures, manifest, paths  # noqa: E402


def resolve_root(arg_root: str | None) -> Path:
    return Path(arg_root).resolve() if arg_root else paths.find_repo_root()


def _next_step(cfg, m: manifest.Manifest) -> str:
    nxt = m.next_gate()
    if nxt is None:
        return "all gates passed: unit complete."
    cmd = cfg.gate_command(nxt, m.slug)
    return f"next gate '{nxt}': {cmd}" if cmd else f"next gate: {nxt}"


def _active_blocks(root: Path, cfg, active: dict) -> bool:
    # A paused holder of .active does not block: pause keeps the pointer only
    # so proceed knows where to return. Anything else blocks.
    try:
        return manifest.load_for(root, cfg, active["slug"]).status != "paused"
    except (manifest.ManifestError, FileNotFoundError):
        return True


def _load_or_hint(root: Path, cfg, slug: str) -> manifest.Manifest:
    try:
        return manifest.load_for(root, cfg, slug)
    except FileNotFoundError:
        print(f"error: no {cfg.artifact_noun} named {slug!r}, check the slug "
              f"(python3 harness/core/gate.py status) or start it "
              f"(python3 harness/core/gate.py start --unit {slug})",
              file=sys.stderr)
        raise SystemExit(1)
    except manifest.ManifestError as exc:
        print(f"error: manifest for {slug} is corrupt ({exc}). "
              f"Run: python3 harness/core/gate.py repair --unit {slug}",
              file=sys.stderr)
        raise SystemExit(1)


def cmd_start(root: Path, cfg, args) -> int:
    active = paths.read_active(root, cfg)
    if active and active.get("slug") != args.unit and _active_blocks(root, cfg, active):
        print(f"error: another {cfg.artifact_noun} is already active: {active['slug']} "
              f"(pause or abandon it first: "
              f"python3 harness/core/gate.py pause --unit {active['slug']})",
              file=sys.stderr)
        return 1
    if active and active.get("slug") != args.unit:
        print(f"note: taking over from paused {active['slug']} "
              f"(proceed later: python3 harness/core/gate.py proceed --unit {active['slug']})")
    unit = paths.unit_path(root, cfg, args.unit)
    mp = paths.manifest_path(root, cfg, args.unit)
    if mp.exists():
        # start must NEVER wipe earned gate evidence: an agent re-issuing the
        # workflow's first command after a context reset is routine.
        try:
            m = manifest.load_for(root, cfg, args.unit)
        except manifest.ManifestError as exc:
            print(f"error: {args.unit} already exists but its manifest is "
                  f"corrupt ({exc}). Run: python3 harness/core/gate.py repair "
                  f"--unit {args.unit}", file=sys.stderr)
            return 1
        if m.status in ("abandoned", "escalated"):
            print(f"error: {args.unit} is {m.status} (terminal), start a "
                  f"fresh {cfg.artifact_noun} under a new slug.",
                  file=sys.stderr)
            return 1
        m.status = "in_progress"
        m.save(mp)
        paths.write_active(root, cfg, args.unit, args.session_id)
        paths.touch_marker(root, args.session_id)
        print(f"{args.unit} already exists, proceeding it (gate evidence "
              f"preserved). {_next_step(cfg, m)}")
        return 0
    unit.mkdir(parents=True, exist_ok=True)
    m = manifest.Manifest.new(args.unit, unit, cfg.gates, cfg.hashed_gates,
                              title=args.title or "")
    m.save(mp)
    paths.write_active(root, cfg, args.unit, args.session_id)
    paths.touch_marker(root, args.session_id)  # arm the Stop gate this session
    print(f"started {args.unit}. {_next_step(cfg, m)}")
    return 0


def cmd_approve(root: Path, cfg, args) -> int:
    if args.gate in cfg.tool_set_gates:
        cmd = cfg.gate_command(args.gate, args.unit)
        print(f"error: gate {args.gate!r} is set only by the tool that produces "
              f"its evidence, run: {cmd}", file=sys.stderr)
        failures.record(root, "gate_refusal", unit=args.unit, gate=args.gate,
                        reason="tool-set gate, manual approve refused")
        return 1
    m = _load_or_hint(root, cfg, args.unit)
    try:
        m.set_gate(args.gate, approved_by=(args.by or "user"), note=(args.note or ""))
    except manifest.ManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        failures.record(root, "gate_refusal", unit=args.unit, gate=args.gate,
                        reason=str(exc))
        return 1
    m.save(paths.manifest_path(root, cfg, args.unit))
    paths.touch_marker(root, _active_session(root, cfg, args.unit))
    print(f"gate {args.gate} set for {args.unit}. {_next_step(cfg, m)}")
    return 0


def _active_session(root: Path, cfg, slug: str) -> str | None:
    active = paths.read_active(root, cfg)
    if active and active.get("slug") == slug:
        return active.get("session_id")
    return None


def cmd_status(root: Path, cfg, args) -> int:
    slug = args.unit
    if not slug:
        active = paths.read_active(root, cfg)
        if not active:
            print(f"no active {cfg.artifact_noun}.")
            return 0
        slug = active["slug"]
    m = _load_or_hint(root, cfg, slug)
    eff = m.effective_gates()
    print(f"{cfg.artifact_noun}: {slug}" + (f": {m.title}" if m.title else ""))
    print(f"  status:       {m.status}")
    print(f"  gates passed: {', '.join(eff) if eff else '(none)'}")
    print(f"  {_next_step(cfg, m)}")
    stale = m.stale_gates()
    if stale:
        print(f"  STALE (files changed since): {', '.join(stale)}, "
              f"re-run the producing tool")
    return 0


def cmd_pause(root: Path, cfg, args) -> int:
    m = _load_or_hint(root, cfg, args.unit)
    m.status = "paused"
    m.save(paths.manifest_path(root, cfg, args.unit))
    print(f"paused {args.unit}. The Stop gate will not block. "
          f"Proceed: python3 harness/core/gate.py proceed --unit {args.unit}")
    return 0


def cmd_proceed(root: Path, cfg, args) -> int:
    active = paths.read_active(root, cfg)
    if active and active.get("slug") != args.unit and _active_blocks(root, cfg, active):
        print(f"error: another {cfg.artifact_noun} is already active: {active['slug']}",
              file=sys.stderr)
        return 1
    m = _load_or_hint(root, cfg, args.unit)
    if m.status in ("abandoned", "escalated"):
        print(f"error: {args.unit} is {m.status} (terminal), start a fresh "
              f"{cfg.artifact_noun} instead.", file=sys.stderr)
        return 1
    m.status = "in_progress"
    m.save(paths.manifest_path(root, cfg, args.unit))
    paths.write_active(root, cfg, args.unit, args.session_id)
    print(f"proceeded {args.unit}. {_next_step(cfg, m)}")
    return 0


def cmd_abandon(root: Path, cfg, args) -> int:
    try:
        m = manifest.load_for(root, cfg, args.unit)
        m.status = "abandoned"
        m.save(paths.manifest_path(root, cfg, args.unit))
    except (manifest.ManifestError, FileNotFoundError):
        pass
    active = paths.read_active(root, cfg)
    if active and active.get("slug") == args.unit:
        paths.clear_active(root, cfg)
    print(f"abandoned {args.unit}.")
    return 0


def cmd_escalate(root: Path, cfg, args) -> int:
    m = _load_or_hint(root, cfg, args.unit)
    m.escalate(args.note)
    m.save(paths.manifest_path(root, cfg, args.unit))
    # The written "why I'm stuck" evidence: note + the failure trail.
    eff = m.effective_gates()
    note = paths.unit_path(root, cfg, args.unit) / "escalation.md"
    note.write_text(
        f"# Escalation: {args.unit}\n\n"
        f"- when: {m.data['escalation']['at']}\n"
        f"- gates passed: {', '.join(eff) if eff else '(none)'}\n"
        f"- next unpassed gate: {m.next_gate() or '(none)'}\n\n"
        f"## Why I'm stuck\n\n{args.note}\n",
        encoding="utf-8")
    active = paths.read_active(root, cfg)
    if active and active.get("slug") == args.unit:
        paths.clear_active(root, cfg)
    failures.record(root, "escalation", unit=args.unit, note=args.note,
                    gates_passed=eff)
    print(f"escalated {args.unit} (terminal). Evidence: {note.relative_to(root)}")
    return 0


def cmd_repair(root: Path, cfg, args) -> int:
    mp = paths.manifest_path(root, cfg, args.unit)
    unit = paths.unit_path(root, cfg, args.unit)
    if not unit.exists():
        print(f"error: no {cfg.artifact_noun} dir for {args.unit}", file=sys.stderr)
        return 1
    try:
        m = manifest.Manifest.load(mp, cfg.gates, cfg.hashed_gates)
        m.save(mp)
        print(f"manifest for {args.unit} is valid, normalized.")
        return 0
    except (manifest.ManifestError, FileNotFoundError):
        pass
    if mp.exists():
        backup = mp.with_suffix(".yaml.corrupt")
        shutil.copy2(mp, backup)
        print(f"backed up corrupt manifest -> {backup.name}")
    m = manifest.Manifest.new(args.unit, unit, cfg.gates, cfg.hashed_gates,
                              title="(recovered)")
    # Gates are intentionally NOT reconstructed, evidence must be re-earned
    # by re-running the producing tools.
    m.save(mp)
    print(f"rebuilt manifest for {args.unit}. All gates must be re-earned. "
          f"{_next_step(cfg, m)}")
    return 0


def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", default=None)
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start", parents=[common])
    s.add_argument("--unit", required=True)
    s.add_argument("--title")
    s.add_argument("--session-id", dest="session_id")

    a = sub.add_parser("approve", parents=[common])
    a.add_argument("gate")
    a.add_argument("--unit", required=True)
    a.add_argument("--note")
    a.add_argument("--by")

    st = sub.add_parser("status", parents=[common])
    st.add_argument("--unit")

    e = sub.add_parser("escalate", parents=[common])
    e.add_argument("--unit", required=True)
    e.add_argument("--note", required=True,
                   help="the written 'why I'm stuck' evidence (mandatory)")

    for name in ("pause", "proceed", "abandon", "repair"):
        q = sub.add_parser(name, parents=[common])
        q.add_argument("--unit", required=True)
        if name == "proceed":
            q.add_argument("--session-id", dest="session_id")

    args = p.parse_args(argv)
    slug = getattr(args, "unit", None)
    if slug is not None and not paths.valid_slug(slug):
        print(f"error: invalid unit slug {slug!r} (letters/digits/._- only, "
              f"no path separators)", file=sys.stderr)
        return 1
    root = resolve_root(args.root)
    try:
        cfg = config.load(root)
    except config.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    dispatch = {
        "start": cmd_start, "approve": cmd_approve, "status": cmd_status,
        "pause": cmd_pause, "proceed": cmd_proceed, "abandon": cmd_abandon,
        "escalate": cmd_escalate, "repair": cmd_repair,
    }
    if args.cmd == "status":  # read-only, never blocked by the lock
        return cmd_status(root, cfg, args)
    # One mutating gate session per project root (advisory flock; item 5).
    try:
        with paths.session_lock(root, cfg):
            return dispatch[args.cmd](root, cfg, args)
    except paths.LockHeld as exc:
        print(f"error: another session is mutating this project's "
              f"{cfg.artifact_noun}s right now (gate lock {exc}). "
              f"One session per project root; retry when it finishes.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
