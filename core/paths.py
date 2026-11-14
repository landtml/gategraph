"""Version-1 name ``paths`` (brief M3). An import shim over core/compat/shims/paths.py, plus
``marker_path``, which delegates to the kernel (core/kernel/paths.py): one marker location.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.kernel.paths import *  # noqa: E402,F401,F403  (kernel names; the shim names below win)
from core.compat.shims.paths import *  # noqa: E402,F401,F403
from core.kernel import paths as _kpaths  # noqa: E402


def marker_path(root, session_id):
    return _kpaths.marker_path(root, session_id)


if __name__ == "__main__":
    raise SystemExit("paths.py is an import shim, not a CLI: import core.kernel.paths")
