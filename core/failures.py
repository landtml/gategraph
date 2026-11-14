#!/usr/bin/env python3
"""failures.py, the version-1 failure ledger command (D1): a script shim over core/kernel/ledger.py
(record, show, escalations, label). The T2 `report` has no replacement yet and refuses with a next: line.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.compat.shims.failures import *  # noqa: E402,F401,F403


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["report"]:
        print("error: failures.py report (the T2 triage) has no replacement yet\n"
              "next: .venv/bin/python harness/core/kernel/ledger.py show --last 50", file=sys.stderr)
        return 1
    from core.kernel.ledger import main as ledger_main
    return ledger_main(argv)


if __name__ == "__main__":
    sys.exit(main())
