#!/usr/bin/env python3
"""Compat shim (X1 D2): maintenance rituals live in core/kernel/maintenance.py.

The stamped `make maintenance` target (seed/Makefile) runs this path.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.kernel.maintenance import *  # noqa: E402,F401,F403
from core.kernel.maintenance import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
