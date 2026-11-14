"""Version-1 ``policy``: compile and scan the deny policies, and the protected-path test
(plan 1.18; brief M3). Delegates to core/kernel/policy.py.
"""
from __future__ import annotations

from fnmatch import fnmatch
from pathlib import Path

from core.compat.shims import deprecated
from core.kernel import policy as _kpolicy


def compile_policies(cfg) -> list:
    deprecated("policy.compile_policies", "core.kernel.policy.compile_policies")
    return _kpolicy.compile_policies(cfg.kernel)


def scan_text(text: str, policies: list, rel_path: str) -> list:
    """Findings as version-1 tuples: (policy id, severity, line number, summary)."""
    deprecated("policy.scan_text", "core.kernel.policy.scan_text")
    return [_tuple(f) for f in _kpolicy.scan_text(text, policies, rel_path)]


def scan_file(path, root, policies: list) -> list:
    deprecated("policy.scan_file", "core.kernel.policy.scan_path", root)
    return [_tuple(f) for f in _kpolicy.scan_path(Path(path), root, policies).findings]


def is_protected(cfg, rel_path: str) -> bool:
    deprecated("policy.is_protected", "the guard's protected_paths rule")
    rel = Path(rel_path).as_posix()
    return any(fnmatch(rel, pattern) for pattern in cfg.protected_paths)


def _tuple(finding) -> tuple:
    return (finding.rule, finding.severity.value, finding.line, finding.message)
