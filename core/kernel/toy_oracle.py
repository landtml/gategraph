"""ToyOracle: a data-free, deterministic oracle for exercising the search end to end.

It implements the ``Oracle`` contract but needs no project data and no real compute, so the
mock-worker tests can walk the full pipeline in milliseconds with zero spend. The variant's own
``variant.py`` may script the outcome through optional module attributes:

- ``TOY_PASSES``     dict merged over the default passes (default: all True)
- ``TOY_METRICS``    dict merged over the default metrics
- ``TOY_TRIPWIRES``  dict of tripwire flags (True = tripped, the variant is void)
- ``TOY_NOTES``      free-text notes on the result

``run_gates`` still executes the variant module, so a bug surfaces as an oracle exception and
the driver routes it as infrastructure. The toy also serves as a template for a real oracle.

Standard library only.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from core.kernel.oracle import Oracle, OracleGateResult

_DEFAULT_PASSES = {"quality": True, "novelty": True}
_DEFAULT_METRICS = {"score": 1.0}


def _load_module(path: "str | Path"):
    """Import a variant module from a file path under a unique name (no sys.modules clash)."""
    p = Path(path)
    name = f"toy_{p.parent.name}_{p.stem}"
    spec = importlib.util.spec_from_file_location(name, str(p))
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load module spec from {p}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ToyOracle(Oracle):
    def incumbent(self) -> dict:
        return {"score": 0.5}

    def run_gates(self, variant_path: str, cfg: dict) -> OracleGateResult:
        mod = _load_module(variant_path)  # exceptions propagate and route as infrastructure
        passes = dict(_DEFAULT_PASSES)
        passes.update(getattr(mod, "TOY_PASSES", {}) or {})
        metrics = dict(_DEFAULT_METRICS)
        metrics.update(getattr(mod, "TOY_METRICS", {}) or {})
        tripwires = dict(getattr(mod, "TOY_TRIPWIRES", {}) or {})
        return OracleGateResult(
            variant_id=Path(variant_path).parent.name,
            passes=passes,
            metrics=metrics,
            tripwires=tripwires,
            notes=str(getattr(mod, "TOY_NOTES", "")),
        )

    def proposal_space(self) -> "list[str]":
        return ["toy-alpha", "toy-beta", "toy-gamma"]

    def soft_passes(self) -> "list[str]":
        # Legacy order from core/loop/decision.py. A concrete oracle declares its own soft passes.
        return ["mechanism_lead", "robustness"]
