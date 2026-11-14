"""Version-1 ``failures``: append one row to the failure ledger (plan 1.18; brief M3). Delegates
to core/kernel/ledger.py, which never raises on an I/O error.
"""
from __future__ import annotations

from core.compat.shims import deprecated
from core.kernel import ledger as _ledger


def record(root, kind: str, **fields) -> dict:
    deprecated("failures.record", "core.kernel.ledger.record", root)
    return _ledger.record(root, kind, **fields)
