#!/usr/bin/env python3
"""Compat shim (X1 D2): generated doc blocks are rendered by core/kernel/gen.py.

Stamped projects regenerate with `python3 harness/core/gen.py` (the kernel's own
`python3 -m core.kernel.gen` is the same run); this path keeps that command working.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.kernel.gen import *  # noqa: E402,F401,F403
from core.kernel.gen import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
