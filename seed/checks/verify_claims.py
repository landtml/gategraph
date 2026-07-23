#!/usr/bin/env python3
"""verify_claims.py, reference REQUIRED-claims check (adapter check pattern).

The bug this pattern prevents (F6, observed in the consumer-C consumer):
a check that verifies only the claim lines that PARSE lets a typo'd
(`clam:`) or deleted claim line skip verification entirely and still earn
the gate. Absence must fail, not skip.

Memo convention:  lines of the form `claim: <name> = <number>`.

Usage (wire in adapter.yaml `checks:`, one --require per mandatory claim;
{files} is lint's space-joined matched-paths substitution):
  python3 harness/adapter/checks/verify_claims.py {files} \
      --require net_return_pct --require max_drawdown_pct \
      [--actual net_return_pct=22.8 ...] [--tolerance 1e-6]

--actual values normally come from your project's recompute step (a wrapper
script that runs the backtest/solver and passes the real numbers). Without
an --actual for a claim, only its PRESENCE is enforced. That is the half
that silently rotted in the wild.

Exit codes: 0 all required claims present (and matching where an actual is
given) / 1 otherwise. Stdlib only.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

CLAIM_RE = re.compile(r"^claim:\s*(\w+)\s*=\s*(-?\d+(?:\.\d+)?)\s*$", re.M)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("memos", nargs="+",
                   help="the document(s) making the claims; every matched "
                        "file must carry the full required set")
    p.add_argument("--require", action="append", default=[], metavar="NAME",
                   help="claim that MUST be present (repeatable)")
    p.add_argument("--actual", action="append", default=[], metavar="NAME=VALUE",
                   help="ground-truth value to verify a claim against (repeatable)")
    p.add_argument("--tolerance", type=float, default=1e-6)
    args = p.parse_args(argv)

    failed = False
    for memo in args.memos:
        text = Path(memo).read_text(encoding="utf-8", errors="replace")
        claims = {m.group(1): float(m.group(2)) for m in CLAIM_RE.finditer(text)}
        missing = [name for name in args.require if name not in claims]
        if missing:
            failed = True
            print(f"verify_claims: {memo}: MISSING required claim(s): "
                  f"{', '.join(missing)}: a deleted or typo'd claim line "
                  f"must fail the gate, not skip verification.")
        for pair in args.actual:
            name, _, raw = pair.partition("=")
            actual = float(raw)
            if name in claims and abs(claims[name] - actual) > args.tolerance:
                failed = True
                print(f"verify_claims: {memo}: REFUTED {name}: memo claims "
                      f"{claims[name]}, recomputed actual is {actual}")

    if not failed:
        print(f"verify_claims: OK, {len(args.require)} required claim(s) "
              f"present" + (f", {len(args.actual)} verified" if args.actual else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
