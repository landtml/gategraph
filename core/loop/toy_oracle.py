"""ToyOracle, a data-free, deterministic oracle for exercising the driver end-to-end.

It implements the :class:`Oracle` contract but needs no project data and no real compute, so
the mock-worker e2e tests (and ``--mock-workers`` CLI runs) can walk the full
``BRIEF → … → RECORD`` pipeline in milliseconds with zero spend. The variant's own
``variant.py`` may **script the outcome** via optional module attributes, letting a test
drive any branch (GRADUATE / below-bar REJECT / tripwire REJECT) deterministically:

- ``TOY_PASSES``     → dict merged over the default passes (default: all True)
- ``TOY_METRICS``    → dict merged over the default metrics
- ``TOY_TRIPWIRES``  → dict of tripwire flags (True = tripped → REJECT, skip CHECK)
- ``TOY_NOTES``      → free-text notes on the GateResult

``run_gates`` still *executes* the variant module (a bug surfaces as an oracle exception
the driver routes as infra), keeping the toy path structurally honest.
A reference implementation of the ``Oracle`` contract (plan §8.2), doubling as a template
for wiring up a project's real oracle.
"""
from __future__ import annotations

from pathlib import Path

from core.loop.oracle import Oracle
from core.loop.schemas import GateResult
from core.loop.smoke import _load_module

_DEFAULT_PASSES = {"quality": True, "novelty": True}
_DEFAULT_METRICS = {"score": 1.0}


class ToyOracle(Oracle):
    def incumbent(self) -> dict:
        return {"score": 0.5}

    def run_gates(self, variant_path: str, cfg: dict) -> GateResult:
        mod = _load_module(variant_path)  # exceptions propagate → routed as infra
        passes = dict(_DEFAULT_PASSES)
        passes.update(getattr(mod, "TOY_PASSES", {}) or {})
        metrics = dict(_DEFAULT_METRICS)
        metrics.update(getattr(mod, "TOY_METRICS", {}) or {})
        tripwires = dict(getattr(mod, "TOY_TRIPWIRES", {}) or {})
        return GateResult(
            variant_id=Path(variant_path).parent.name,
            passes=passes,
            metrics=metrics,
            tripwires=tripwires,
            notes=str(getattr(mod, "TOY_NOTES", "")),
        )

    def proposal_space(self) -> "list[str]":
        return ["toy-alpha", "toy-beta", "toy-gamma"]
