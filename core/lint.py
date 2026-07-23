#!/usr/bin/env python3
"""lint.py, policy lint + adapter checks + wikilink integrity.

  lint.py --unit <slug> [--set-gate <gate>]   scan the unit's files with the
      adapter deny policies and run matching adapter checks. With --set-gate
      and no fatal findings, records the gate with a sha256 evidence map (the
      standard way a tool_set gate is earned).
  lint.py --file <f>                          scan a single file (no gate).
  lint.py refs                                wikilink integrity across docs.

Exit codes: 0 clean / 1 advisory-only / 2 fatal (or gate could not be set).
Adapter check contract (plan §6b): exit 0 pass / 1 fatal (warn when the
check declares severity: warn) / 2 warn / anything else fatal "did not run".
"""
from __future__ import annotations

import argparse
from fnmatch import fnmatch
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import config, failures, manifest, paths, policy  # noqa: E402

WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
DOC_GLOBS = ["CLAUDE.md", "README.md", "MEMORY.md", "HANDOFF.md",
             "docs/**/*.md", ".claude/**/*.md"]
# Files in a unit dir that are harness bookkeeping, not lintable artifacts.
# Shared with the staleness model: what lint scans is exactly what a hashed
# gate must cover (F36).
UNIT_EXCLUDE = manifest.UNIT_EXCLUDE
CHECK_TIMEOUT = 120


def resolve_root(arg_root: str | None) -> Path:
    return Path(arg_root).resolve() if arg_root else paths.find_repo_root()


def unit_files(root: Path, cfg, slug: str) -> list[Path]:
    unit = paths.unit_path(root, cfg, slug)
    if not unit.exists():
        return []
    return sorted(p for p in unit.rglob("*")
                  if p.is_file() and p.name not in UNIT_EXCLUDE)


def run_adapter_checks(root: Path, cfg, files: list[Path], slug: str | None,
                       budget_s: "float | None" = None) -> tuple[list, list]:
    """Run each adapter check whose `on` globs match any of `files`.

    Returns (fatal, warn) finding lines. Substitutions in cmd:
    {files} -> space-joined matched repo-relative paths, {slug}/{unit} -> slug.

    ``budget_s`` (hook context only, gate-earning runs must NOT pass it): a
    total wall-clock budget across all checks. A check that can't start within
    it is skipped with an EXPLICIT warn pointing at `make check`, honest
    degradation instead of the hook host silently killing the whole hook and
    delivering nothing (F31). Each check's timeout is capped by what remains.
    """
    fatal, warn = [], []
    deadline = None if budget_s is None else time.monotonic() + budget_s
    rels = []
    for f in files:
        try:
            rels.append(f.resolve().relative_to(root.resolve()).as_posix())
        except ValueError:
            continue  # out-of-tree file: policies scan it; repo-scoped checks cannot
    for check in cfg.checks:
        matched = [r for r in rels
                   if any(policy.scope_match([g], r) for g in check.get("on") or [])]
        if not matched:
            continue
        cmd = str(check["cmd"])
        cmd = cmd.replace("{files}", " ".join(shlex.quote(r) for r in matched))
        for token in ("{slug}", "{unit}"):
            cmd = cmd.replace(token, slug or "")
        timeout_s = CHECK_TIMEOUT
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining < 1.0:
                warn.append(
                    f"[{check['id']}] SKIPPED in hook context (time budget exhausted), "
                    f"run `make check` for full enforcement")
                continue
            timeout_s = min(CHECK_TIMEOUT, remaining)
        try:
            proc = subprocess.run(cmd, shell=True, cwd=root, capture_output=True,
                                  text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            fatal.append(f"[{check['id']}] check timed out after {timeout_s:.0f}s")
            continue
        detail = (proc.stderr.strip() or proc.stdout.strip() or "").splitlines()
        detail = "; ".join(detail[:3])
        suffix = f": {detail}" if detail else ""
        if proc.returncode == 0:
            continue
        if proc.returncode == 1:
            line = f"[{check['id']}] exit 1{suffix}"
            # checks[].severity: warn lets a check report findings without
            # blocking; the default (and 'fatal') blocks.
            (warn if check.get("severity") == "warn" else fatal).append(line)
        elif proc.returncode == 2:
            warn.append(f"[{check['id']}] exit 2{suffix}")
        else:
            # 127/126, signals, anything off-contract: the check DID NOT RUN.
            # A gate must never be earned on a check that never executed.
            fatal.append(f"[{check['id']}] check did not run "
                         f"(exit {proc.returncode}){suffix}")
    return fatal, warn


def scan_files(root: Path, cfg, files: list[Path]) -> tuple[list, list]:
    pols = policy.compile_policies(cfg)
    fatal, warn = [], []
    for f in files:
        if not f.exists():
            continue
        for pid, sev, ln, summary in policy.scan_file(f, root, pols):
            line = f"{f.name}:{ln}  [{pid}] {summary}"
            (fatal if sev == "fatal" else warn).append(line)
    return fatal, warn


def report(fatal: list, warn: list) -> None:
    for x in fatal:
        print(f"  ✗ {x}", file=sys.stderr)
    for x in warn:
        print(f"  · {x}")


def _track_fail_streak(root: Path, slug: str, failed: bool) -> int:
    """Consecutive fatal lint runs for a unit, the Layer-1 escalation signal
    (plan §12): stop_gate escalates when it reaches the adapter threshold."""
    p = root / "harness" / "state" / f"failstreak-{slug}"
    try:
        streak = int(p.read_text().strip())
    except (OSError, ValueError):
        streak = 0
    streak = streak + 1 if failed else 0
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(streak), encoding="utf-8")
    return streak


def cmd_scan(root: Path, cfg, args) -> int:
    if args.file:
        files = [Path(args.file).resolve()]
        slug = None
    else:
        slug = args.unit
        if not paths.valid_slug(slug):
            print(f"error: invalid unit slug {slug!r} (letters/digits/._- only, "
                  f"no path separators)", file=sys.stderr)
            return 2
        if not paths.unit_path(root, cfg, slug).exists():
            print(f"error: no {cfg.artifact_noun} named {slug!r}, check the "
                  f"slug: python3 harness/core/gate.py status", file=sys.stderr)
            return 2
        files = unit_files(root, cfg, slug)
    fatal, warn = scan_files(root, cfg, files)
    cf, cw = run_adapter_checks(root, cfg, files, slug)
    fatal += cf
    warn += cw
    report(fatal, warn)

    if slug is not None:
        streak = _track_fail_streak(root, slug, bool(fatal))
        threshold = cfg.escalation.get("fail_streak_to_escalate")
        if fatal and threshold and streak >= threshold:
            print(f"lint: {streak} consecutive failures for {slug}, at the "
                  f"escalation threshold. Consider: python3 harness/core/gate.py "
                  f"escalate --unit {slug} --note '...'", file=sys.stderr)

    if fatal:
        if args.set_gate:  # a gate attempt failed, that's a ledger row (§13)
            failures.record(root, "lint_fatal", unit=slug, gate=args.set_gate,
                            findings=fatal[:10])
        print(f"\nlint: FAILED ({len(fatal)} fatal, {len(warn)} advisory)."
              + (f" Gate '{args.set_gate}' NOT set." if args.set_gate else ""),
              file=sys.stderr)
        return 2

    if args.set_gate:
        if slug is None:
            print("error: --set-gate requires --unit", file=sys.stderr)
            return 2
        if not files:
            # F36: a gate earned over ZERO files is vacuous, no check ran,
            # nothing would be hashed, and staleness could never reopen it.
            print(f"error: cannot set gate {args.set_gate!r}: {slug!r} has no "
                  f"files to certify. Write the {cfg.artifact_noun}'s "
                  f"artifacts first, then re-run this command.",
                  file=sys.stderr)
            failures.record(root, "gate_refusal", unit=slug,
                            gate=args.set_gate,
                            reason="empty file set, nothing to certify")
            return 2
        try:
            m = manifest.load_for(root, cfg, slug)
            hashes = {f.relative_to(paths.unit_path(root, cfg, slug)).as_posix():
                      manifest.sha256_file(f) for f in files if f.exists()}
            m.set_gate(args.set_gate, sha256=hashes)
            m.save(paths.manifest_path(root, cfg, slug))
        except FileNotFoundError:
            print(f"lint: clean, but no manifest for {slug!r}: start it first: "
                  f"python3 harness/core/gate.py start --unit {slug}",
                  file=sys.stderr)
            return 2
        except manifest.ManifestError as exc:
            print(f"lint: clean, but could not set {args.set_gate!r}: {exc}",
                  file=sys.stderr)
            return 2
        nxt = m.next_gate()
        step = f" Next: {cfg.gate_command(nxt, slug)}" if nxt else " Unit complete."
        print(f"lint: {'clean' if not warn else f'{len(warn)} advisory'}; "
              f"{args.set_gate} set for {slug}.{step}")
    else:
        print(f"lint: {'clean' if not warn else f'{len(warn)} advisory'}.")
    return 1 if warn else 0


def cmd_refs(root: Path, cfg, args) -> int:
    """Built-in wikilink integrity: every [[anchor]] must resolve to a doc stem."""
    docs_lint = cfg.docs_lint if hasattr(cfg, "docs_lint") else {}
    extra = docs_lint.get("extra_globs") or []
    exclude = docs_lint.get("exclude_globs") or []
    seen, docs = set(), []
    for pat in [*DOC_GLOBS, *extra]:
        for f in sorted(root.glob(pat)):
            if not f.is_file() or f in seen:
                continue
            seen.add(f)
            rel = f.relative_to(root).as_posix()
            if any(fnmatch(rel, e) for e in exclude):
                continue
            docs.append(f)
    stems = {f.stem for f in docs}
    advisory = []
    for doc in docs:
        in_fence = False
        for n, line in enumerate(
                doc.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            for anchor in WIKILINK_RE.findall(line):
                anchor = anchor.strip()
                if anchor not in stems:
                    advisory.append(
                        f"{doc.relative_to(root)}:{n}  [[{anchor}]] resolves to no doc")
    for x in advisory:
        print(f"  · {x}")
    if advisory:
        print(f"\nlint refs: {len(advisory)} unresolved wikilink(s) (advisory).")
        return 1
    print("lint refs: clean.")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", nargs="?", choices=["scan", "refs"], default="scan")
    p.add_argument("--unit")
    p.add_argument("--file")
    p.add_argument("--set-gate", dest="set_gate")
    p.add_argument("--root", default=None)
    args = p.parse_args(argv)
    root = resolve_root(args.root)
    try:
        cfg = config.load(root)
    except config.ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.mode == "refs":
        return cmd_refs(root, cfg, args)
    if not args.unit and not args.file:
        p.error("scan needs --unit or --file")
    return cmd_scan(root, cfg, args)


if __name__ == "__main__":
    sys.exit(main())
