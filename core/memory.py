#!/usr/bin/env python3
"""Compat shim (X1 D2): the typed memory stores live in core/kernel/memory.py.

The session and stop hooks reach memory through the kernel; this path stays so the
stamped `harness/core/memory.py fact` text keeps working.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.kernel.memory import *  # noqa: E402,F401,F403
from core.kernel.memory import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
