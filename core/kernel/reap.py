"""Reaping: delete stale runtime state past its age; refuse when runtime state
has reached git history (state clutter in git is a harness bug, not litter).

The state tree (brand.STATE_DIRNAME, and the legacy directory names) must never
be git-tracked, whatever the patterns say. Patterns are passed by the caller;
the Config contract does not yet carry state_reap.

Usage: python3 -m core.kernel.reap [--dry-run] [--root DIR]
Exit codes: 0 ok / 1 committed state detected (nothing is deleted then either).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from core import brand
from core.kernel import paths

UNTRACKABLE = (brand.STATE_DIRNAME, *brand.LEGACY_STATE_DIRNAMES)
DEFAULT_PATTERNS = ("**/*.pid",)
DEFAULT_MAX_AGE_DAYS = 7.0


@dataclass
class ReapResult:
    exit_code: int
    committed: list[str] = field(default_factory=list)
    victims: list[Path] = field(default_factory=list)
    removed: list[Path] = field(default_factory=list)


def committed_state(root: Path, patterns) -> list[str]:
    """Git-tracked files that live in the state tree or match the patterns."""
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "--",
                              *UNTRACKABLE, *patterns],
                             capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []  # not a git repository: nothing to check
    return [line for line in out.stdout.splitlines() if line.strip()]


def stale_files(root: Path, patterns, max_age_days: float,
                now: float | None = None) -> list[Path]:
    cutoff = (time.time() if now is None else now) - max_age_days * 86400
    out: list[Path] = []
    seen: set[Path] = set()
    for pattern in patterns:
        for f in sorted(Path(root).glob(pattern)):
            if f.is_file() and f not in seen and f.stat().st_mtime < cutoff:
                seen.add(f)
                out.append(f)
    return out


def reap(root, patterns=DEFAULT_PATTERNS, max_age_days: float = DEFAULT_MAX_AGE_DAYS,
         dry_run: bool = False) -> ReapResult:
    root = Path(root)
    tracked = committed_state(root, list(patterns))
    if tracked:
        return ReapResult(exit_code=1, committed=tracked)
    victims = stale_files(root, patterns, max_age_days)
    removed: list[Path] = []
    if not dry_run:
        for f in victims:
            f.unlink()
            removed.append(f)
    return ReapResult(exit_code=0, victims=victims, removed=removed)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Delete stale runtime state.")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--root", default=None)
    args = p.parse_args(argv)
    root = Path(args.root).resolve() if args.root else paths.find_root()
    result = reap(root, dry_run=args.dry_run)
    if result.exit_code:
        print("reap: ERROR: runtime state is committed to git "
              "(gitignore it and `git rm --cached`):", file=sys.stderr)
        for t in result.committed:
            print(f"  x {t}", file=sys.stderr)
        return result.exit_code
    if not result.victims:
        print("reap: nothing stale.")
        return 0
    for f in result.victims:
        rel = f.relative_to(root)
        print(f"  {'would delete' if args.dry_run else 'deleted'} {rel}")
    print(f"reap: {'would delete' if args.dry_run else 'deleted'} "
          f"{len(result.victims)} file(s) older than {DEFAULT_MAX_AGE_DAYS:g}d.")
    return 0


if __name__ == "__main__":  # run from the repository root: python3 -m core.kernel.reap
    sys.exit(main())
