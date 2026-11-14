"""Version-1 ``doctor``: the cached doctor payload (plan 1.18; brief M3). Delegates to
core/kernel/doctor.py.
"""
from __future__ import annotations

from core.compat.shims import deprecated
from core.kernel import doctor as _kdoctor


def read_cache(root):
    """The cached payload as a dict, or None when there is no usable fresh cache."""
    deprecated("doctor.read_cache", "core.kernel.doctor.read_cache", root)
    payload, _note = _kdoctor.read_cache(root)
    return payload
