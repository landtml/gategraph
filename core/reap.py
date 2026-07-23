#!/usr/bin/env python3
"""reap.py, delete stale runtime state past its max age; error on committed
state (state clutter that reached git history is a harness bug, not litter).

Patterns and max_age_days come from adapter.yaml state_reap.

Usage: reap.py [--dry-run] [--root DIR]
Exit codes: 0 ok / 1 committed state detected (nothing is deleted then either).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import config, paths  # noqa: E402

# Trees that must never be git-tracked, regardless of reap patterns.
UNTRACKABLE = ["harness/state", "harness/logs"]


def committed_state(root: Path, patterns: list[str]) -> list[str]:
    """Git-tracked files that live in state trees or match reap patterns."""
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "--",
                              *UNTRACKABLE, *patterns],
                             capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []  # not a git repo, nothing to check
    return [l for l in out.stdout.splitlines() if l.strip()]


def stale_files(root: Path, patterns: list[str], max_age_days: float) -> list[Path]:
    cutoff = time.time() - max_age_days * 86400
    out = []
    for pattern in patterns:
        for f in sorted(Path(root).glob(pattern)):
            if f.is_file() and f.stat().st_mtime < cutoff:
                out.append(f)
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--root", default=None)
    args = p.parse_args(argv)
    root = Path(args.root).resolve() if args.root else paths.find_repo_root()
    cfg = config.load(root)
    patterns = cfg.state_reap.get("patterns") or []
    max_age = cfg.state_reap.get("max_age_days", 7)

    tracked = committed_state(root, patterns)
    if tracked:
        print("reap: ERROR: runtime state is committed to git "
              "(gitignore it and `git rm --cached`):", file=sys.stderr)
        for t in tracked:
            print(f"  ✗ {t}", file=sys.stderr)
        return 1

    victims = stale_files(root, patterns, max_age)
    if not victims:
        print("reap: nothing stale.")
        return 0
    for f in victims:
        rel = f.relative_to(root)
        if args.dry_run:
            print(f"  would delete {rel}")
        else:
            f.unlink()
            print(f"  deleted {rel}")
    print(f"reap: {'would delete' if args.dry_run else 'deleted'} "
          f"{len(victims)} file(s) older than {max_age}d.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
