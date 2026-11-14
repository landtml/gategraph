#!/usr/bin/env python3
"""lint.py, the version-1 lint command (brief D1, D11): a short script shim.

  lint.py refs                 -> core/kernel/selflint.py (its wikilink check runs with the rest)
  lint.py --unit S [--set-gate G] and lint.py --file F  -> refused, exit 1, with a next: line.
  A gate is set only by its attempt (`gate attempt G --unit S`): bug B01.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
NEW = ".venv/bin/python harness/core/cli/gate.py"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", nargs="?", choices=["scan", "refs"], default="scan")
    p.add_argument("--unit")
    p.add_argument("--file")
    p.add_argument("--set-gate", dest="set_gate")
    p.add_argument("--root", default=None)
    args = p.parse_args(sys.argv[1:] if argv is None else argv)
    if args.mode == "refs":
        from core.kernel.selflint import main as selflint_main
        return selflint_main([])
    slug = args.unit or "<slug>"
    if args.set_gate:
        print("error: lint --set-gate is retired: only a gate's own attempt sets it (bug B01)",
              file=sys.stderr)
        print(f"next: {NEW} attempt {args.set_gate} --unit {slug}", file=sys.stderr)
    else:
        print("error: lint scan has no single-command replacement; the runner runs a unit's "
              "checks at each gate attempt", file=sys.stderr)
        print(f"next: {NEW} attempt <gate> --unit {slug}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
