"""Version-1 ``paths``: unit and state locations, the ``.active`` pointer and session markers
(plan 1.18; brief M3). Delegates to core/kernel/paths.py and core/hosts/session.py.
"""
from __future__ import annotations

from pathlib import Path

from core.compat.shims import deprecated
from core.hosts import session as _session
from core.kernel import paths as _kpaths


def find_repo_root(start=None) -> Path:
    deprecated("paths.find_repo_root", "core.kernel.paths.find_root")
    return _kpaths.find_root(start)


def state_dir(root) -> Path:
    deprecated("paths.state_dir", "core.kernel.paths.state_dir")
    return _kpaths.state_dir(root)


def unit_path(root, cfg, slug: str) -> Path:
    deprecated("paths.unit_path", "core.kernel.paths.unit_path")
    return _kpaths.unit_path(root, cfg.unit_dir, slug)


def manifest_path(root, cfg, slug: str) -> Path:
    deprecated("paths.manifest_path", "the unit's manifest.yaml projection")
    return _kpaths.unit_path(root, cfg.unit_dir, slug) / "manifest.yaml"


def read_active(root, cfg):
    deprecated("paths.read_active", "core.hosts.session.read_active")
    return _session.read_active(root, cfg.kernel)


def clear_active(root, cfg) -> None:
    deprecated("paths.clear_active", "the runner, which releases the pointer on a terminal unit")
    _session.active_path(root, cfg.kernel).unlink(missing_ok=True)


def touch_marker(root, session_id) -> None:
    deprecated("paths.touch_marker", "core.kernel.paths.marker_path")
    if not session_id:
        return
    marker = Path(_kpaths.marker_path(root, session_id))
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
