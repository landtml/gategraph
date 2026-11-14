#!/usr/bin/env python3
"""doctor.py, the version-1 toolchain preflight (brief D1): a script shim over core/kernel/doctor.py.

Usage: doctor.py [--root DIR] [--json] [--quiet] [--privacy strict]. Same exit codes as the kernel
doctor (0 when every required check passes, 1 otherwise). The version pin for the claude CLI
(core/loop) is not checked any more: the loop was removed (X1 B2).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.compat.shims.doctor import read_cache  # noqa: E402,F401


def main(argv=None) -> int:
    from core.kernel.doctor import main as kernel_main
    return kernel_main(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
