#!/usr/bin/env python3
"""Compat shim (X1 D2, D7): stale-state reaping lives in core/kernel/reap.py.

tests/test_upgrade_path.py expects this path in the stamped core, so it stays as a script.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.kernel.reap import *  # noqa: E402,F401,F403
from core.kernel.reap import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
