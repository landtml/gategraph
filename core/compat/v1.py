"""The version-1 API in one place (plan 1.18; brief M3).

``from core.compat import v1`` gives the modules a version-1 adapter script imports
(``v1.config``, ``v1.manifest``, ``v1.paths``, ``v1.policy``, ``v1.lint``, ``v1.doctor``,
``v1.failures``), and the names those scripts use directly. The old top-level modules under
``harness/core/`` re-export the same objects (see the shims under ``shims/``).
"""
from __future__ import annotations

from core.compat.shims import config, doctor, failures, gate, lint, manifest, paths, policy
from core.compat.shims.config import Config, ConfigError
from core.compat.shims.manifest import Manifest, ManifestError, load_for, sha256_file
from core.compat.shims.paths import (clear_active, find_repo_root, manifest_path, read_active,
                                     state_dir, touch_marker, unit_path)
from core.compat.shims.policy import compile_policies, is_protected, scan_file, scan_text
from core.compat.shims.lint import run_adapter_checks, scan_files, unit_files
from core.compat.shims.doctor import read_cache
from core.compat.shims.failures import record

__all__ = [
    "config", "doctor", "failures", "gate", "lint", "manifest", "paths", "policy",
    "Config", "ConfigError", "Manifest", "ManifestError", "load_for", "sha256_file",
    "clear_active", "find_repo_root", "manifest_path", "read_active", "state_dir",
    "touch_marker", "unit_path", "compile_policies", "is_protected", "scan_file", "scan_text",
    "run_adapter_checks", "scan_files", "unit_files", "read_cache", "record",
]
