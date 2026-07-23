"""The generic threshold layer, metrics in, booleans out (plan §8.1).

The oracle computes the raw numbers (slow, project-specific, integration-tested);
this module turns them into pass booleans from ``loop.gates.metric_floors``
(fast, unit-tested, config-driven). Domain sign conventions belong in the
project's oracle, a floor here is always "pass when metric >= floor".
"""
from __future__ import annotations

from core.loop.schemas import GateResult


def evaluate_metric_floors(metrics: dict, floors: dict) -> dict:
    """``{metric: floor}`` → ``{metric: bool}``. A missing metric fails its floor."""
    out: dict = {}
    for name, floor in (floors or {}).items():
        value = metrics.get(name)
        out[name] = value is not None and value >= floor
    return out


def apply_metric_floors(gr: GateResult, floors: dict) -> GateResult:
    """Merge floor-derived passes into ``gr``. Floors OWN the pass for their metric
    (config is the threshold authority); oracle-computed passes for other names stay."""
    gr.passes.update(evaluate_metric_floors(gr.metrics, floors))
    return gr
