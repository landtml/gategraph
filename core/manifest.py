"""Version-1 name `manifest` (brief M3). An import shim: the code lives in core/compat/shims/manifest.py,
which reads through the kernel. Removed when the last version-1 caller is gone (X1).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.compat.shims.manifest import *  # noqa: E402,F401,F403


if __name__ == "__main__":
    raise SystemExit("manifest.py is an import shim, not a CLI: import core.compat.shims.manifest or the kernel")
