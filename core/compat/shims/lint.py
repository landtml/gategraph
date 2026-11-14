"""Version-1 ``lint``: unit file listing, policy scans and adapter checks (plan 1.18; brief M3).

``run_adapter_checks`` runs each check whose ``on`` globs match, through the kernel's check runner,
and returns the (fatal, warn) lines version-1 callers print. Only the gate command sets a gate
(bug B01), so there is no ``--set-gate`` here.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from core.compat.shims import deprecated
from core.kernel import checks as _kchecks
from core.kernel import paths as _kpaths
from core.kernel import policy as _kpolicy
from core.kernel.surface import BOOKKEEPING
from core.kernel.types import CheckStatus, Severity

_FAILING = (CheckStatus.FATAL, CheckStatus.DID_NOT_RUN)


def unit_files(root, cfg, slug: str) -> list:
    """The unit's files, sorted, without the bookkeeping the runner writes."""
    deprecated("lint.unit_files", "core.kernel.surface (the unit's surface)", root)
    unit = _kpaths.unit_path(root, cfg.unit_dir, slug)
    if not unit.is_dir():
        return []
    return sorted(p for p in unit.rglob("*") if p.is_file() and p.name not in BOOKKEEPING)


def _relative(root, files) -> list:
    base = os.path.realpath(os.fspath(root))
    rels = []
    for f in files:
        real = os.path.realpath(os.fspath(f))
        if real.startswith(base + os.sep):
            rels.append(Path(real).relative_to(base).as_posix())
    return rels


def run_adapter_checks(root, cfg, files, slug, budget_s=None) -> tuple[list, list]:
    """Run each adapter check whose ``on`` globs match a file. Returns (fatal, warn) lines."""
    deprecated("lint.run_adapter_checks", "core.kernel.checks.run_check", root)
    fatal, warn = [], []
    deadline = None if budget_s is None else time.monotonic() + budget_s
    rels = _relative(root, files)
    for spec in cfg.kernel.checks:
        matched = [r for r in rels if _kpolicy.scope_match(list(spec.on), r)]
        if not matched:
            continue
        result = _kchecks.run_check(spec, root, files=matched, unit=slug or "",
                                    deadline=deadline)
        if result.status is CheckStatus.PASS:
            continue
        detail = "; ".join(f.message for f in result.findings[:3])
        suffix = f": {detail}" if detail else ""
        if result.status in _FAILING:
            fatal.append(f"[{spec.id}] {result.status.value}{suffix}")
        else:
            warn.append(f"[{spec.id}] {result.status.value}{suffix}")
    return fatal, warn


def scan_files(root, cfg, files) -> tuple[list, list]:
    """Scan files against the deny policies. Returns (fatal, warn) lines."""
    deprecated("lint.scan_files", "core.kernel.policy.scan_path", root)
    policies = _kpolicy.compile_policies(cfg.kernel)
    fatal, warn = [], []
    for path in files:
        for finding in _kpolicy.scan_path(path, root, policies).findings:
            line = f"{finding.path or path}:{finding.line}: [{finding.rule}] {finding.message}"
            (fatal if finding.severity is Severity.FATAL else warn).append(line)
    return fatal, warn
