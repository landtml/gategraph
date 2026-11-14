"""Shared helpers for the version-1 shims: the deprecation line and the log location.

Every shim call that a process makes appends one deprecation line to the hooks log, at most once
per process and per shim name, so a script that calls a shim in a loop writes one line.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from core.kernel import paths as _kpaths

SHIM_LINE = "deprecated version-1 shim"
REMOVAL = "removed one minor release after the migration command ships"
_NOTED: set[str] = set()


def _log_root(root=None):
    if root is not None:
        return Path(root)
    try:
        return _kpaths.find_root()
    except Exception:  # no project above the working directory: nothing to log to
        return None


def deprecated(shim: str, replacement: str, root=None) -> None:
    """Append one line for ``shim`` to ``harness/logs/hooks.log`` (once per process). Never raises."""
    if shim in _NOTED:
        return
    _NOTED.add(shim)
    base = _log_root(root)
    if base is None:
        return
    try:
        logdir = Path(base) / "harness" / "logs"
        logdir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().replace(microsecond=0).isoformat()
        line = (f"[{stamp}] {SHIM_LINE}: {shim} called by {Path(sys.argv[0]).name or '-c'}; "
                f"use {replacement}; {REMOVAL}\n")
        with open(logdir / "hooks.log", "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass
