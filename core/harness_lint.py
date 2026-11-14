#!/usr/bin/env python3
"""Compat shim (X1 D2): the harness self-lint lives in core/kernel/selflint.py.

Stamped commands still run this path (seed/Makefile `check`), so it stays as a script
that exits with the kernel's status. Import names come from the kernel module.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.kernel.selflint import *  # noqa: E402,F401,F403
from core.kernel.selflint import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
