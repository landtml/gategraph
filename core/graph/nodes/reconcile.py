"""The reconcile node (plan sections 1.5, 1.7, 1.8).

Runs at the start of every pass and after every seal. It never trusts the
effective or stale values in state: it re-hashes each gate's surface on disk
and recomputes both lists from the evidence chain.

A broken chain, or a surface file that is missing or corrupt, is reported as one
fatal finding (``integrity.chain_broken`` or ``integrity.surface_missing``). ``status``
is left alone, and ``route.next_step`` sends the unit to escalate, so the escalate
node asks a human and records the decision as evidence. A missing surface is never
reported as stale.

Integrity is recomputed on every pass. Old ``integrity.*`` findings are removed when
integrity passes, or replaced by the current one when it still fails, so a repaired
chain stops escalating and a failing one never duplicates its finding. Every other
finding is kept in order. ``findings`` is written only when the list changes.

Config digest: the contract gives reconcile no writable flag key, so a mismatch
between the pinned ``config_digest`` and the current one is not written here.
``route.next_step`` compares the same two digests and escalates first.

Pure in the chain, the files on disk and the surface store. Standard library and
core.kernel only; the node never pauses the graph and imports no LangGraph.
"""
from __future__ import annotations

import functools
from collections.abc import Mapping
from typing import Any

from core.kernel.evidence import ChainError
from core.kernel.surface import (SurfaceMissing, effective_gates, load_surface,
                                 stale_gates)
from core.kernel.types import Finding, Severity

REMEDY = "run `gate verify --unit <slug>` to see which record or surface is damaged"
INTEGRITY_PREFIX = "integrity."


def cfg_value(cfg: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a mapping or an object attribute; ``default`` if absent."""
    if isinstance(cfg, Mapping):
        return cfg.get(name, default)
    return getattr(cfg, name, default)


def reconcile(state: Mapping[str, Any], cfg: Any) -> dict:
    """Return the reconciled ``effective``, ``stale`` and ``status``."""
    gates = list(cfg_value(cfg, "gates") or ())
    unit_dir = cfg_value(cfg, "unit_dir")
    state_dir = cfg_value(cfg, "state_dir")
    chain = list(state.get("evidence") or [])
    surfaces = functools.partial(load_surface, state_dir)
    old = list(state.get("findings") or [])

    try:
        effective = effective_gates(chain, gates, unit_dir, surfaces)
        stale = stale_gates(chain, gates, unit_dir, surfaces)
    except ChainError as exc:
        return _integrity(old, "integrity.chain_broken",
                          f"evidence chain is broken: {exc}")
    except SurfaceMissing as exc:
        return _integrity(old, "integrity.surface_missing",
                          f"a stored surface is missing or corrupt: {exc}")

    status = state.get("status") or "in_progress"
    update = {"effective": effective, "stale": stale, "status": status}
    kept = _without_integrity(old)
    if kept != old:
        update["findings"] = kept
    return update


def _is_integrity(finding: Any) -> bool:
    """True for a finding whose rule starts with ``integrity.``."""
    return (isinstance(finding, Mapping)
            and str(finding.get("rule") or "").startswith(INTEGRITY_PREFIX))


def _without_integrity(findings: list) -> list:
    """The findings with every ``integrity.*`` one removed, the rest in their order."""
    return [finding for finding in findings if not _is_integrity(finding)]


def _integrity(old: list, rule: str, message: str) -> dict:
    """An integrity failure: no effective or stale gates, status untouched.

    The current integrity finding replaces any old ones; other findings are kept.
    ``findings`` is written only when it differs from ``old``.
    """
    finding = Finding(rule=rule, severity=Severity.FATAL, path=None, line=None,
                      message=message, remedy=REMEDY)
    findings = _without_integrity(old) + [finding.to_dict()]
    update = {"effective": [], "stale": []}
    if findings != old:
        update["findings"] = findings
    return update

