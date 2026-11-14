"""Version-1 name `config` (brief M3). An import shim: the code lives in core/compat/shims/config.py,
which reads through the kernel. Removed when the last version-1 caller is gone (X1).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.compat.shims.config import *  # noqa: E402,F401,F403


if __name__ == "__main__":
    raise SystemExit("config.py is an import shim, not a CLI: import core.compat.shims.config or the kernel")
