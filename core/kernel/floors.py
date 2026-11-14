"""The generic threshold layer: metrics in, booleans out (kernel port of the legacy gates module).

The oracle computes the raw numbers (slow, project-specific). This module turns them into pass
booleans from a config ``{metric: floor}`` map (fast, unit-tested). A floor is always "pass when
metric >= floor". Domain sign conventions belong in the project's oracle.

Standard library only.
"""
from __future__ import annotations

from core.kernel.oracle import OracleGateResult


def evaluate_metric_floors(metrics: dict, floors: dict) -> dict:
    """``{metric: floor}`` -> ``{metric: bool}``. A missing metric fails its floor."""
    out: dict = {}
    for name, floor in (floors or {}).items():
        value = metrics.get(name)
        out[name] = value is not None and value >= floor
    return out


def apply_metric_floors(gr: OracleGateResult, floors: dict) -> OracleGateResult:
    """Merge floor-derived passes into ``gr``. Floors own the pass for their metric (config is
    the threshold authority); oracle-computed passes for other names stay."""
    gr.passes.update(evaluate_metric_floors(gr.metrics, floors))
    return gr
