"""The Oracle contract and the search-side types it produces.

Kernel port of the legacy oracle module. The engine (registry, decision, dedup, floors, brief)
is project-agnostic. Each project supplies ONE oracle that owns the evaluation authority: it
loads the data, computes every deterministic pass and tripwire audit, and returns an
``OracleGateResult``. No model is involved in producing that result.

Names here are deliberately distinct from ``core.kernel.types``:

- ``OracleGateResult`` is the locked oracle's output. ``core.kernel.types`` has no such type.
- ``VariantVerdict`` is the search verdict GRADUATE / PROBATION / REJECT. The
  ``core.kernel.types.Verdict`` enum is the guard verdict (allow / deny / ask) and means
  something else.

Standard library only. This module must not import anything from the legacy loop package.
"""
from __future__ import annotations

import enum
import importlib
import importlib.util
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


class OracleLoadError(Exception):
    """The ``oracle`` spec could not be resolved to a class."""


class VariantVerdict(str, enum.Enum):
    """The 3-state search outcome. Values match the event log's ``verdict`` field."""

    GRADUATE = "GRADUATE"     # all passes + a clear mechanism
    PROBATION = "PROBATION"   # near-miss but structurally promising: re-queue at the failing pass
    REJECT = "REJECT"         # objectively exhausted, or an informative kill


@dataclass
class OracleGateResult:
    """The locked oracle's deterministic output: generic passes plus tripwire flags.

    ``passes`` maps a pass name to a bool. ``metrics`` holds the raw numbers behind each pass.
    ``tripwires`` maps a tripwire name to whether it TRIPPED (True = the variant is void).
    The oracle computes every metric itself, so a variant can never report its own score.
    """

    variant_id: str
    passes: dict
    metrics: dict
    tripwires: dict = field(default_factory=dict)
    notes: str = ""

    @property
    def n_passed(self) -> int:
        return sum(1 for v in self.passes.values() if v)

    @property
    def tripwire_ok(self) -> bool:
        """The non-negotiable safety gate: no tripwire fired."""
        return not any(bool(v) for v in self.tripwires.values())

    def to_dict(self) -> dict:
        return {"kind": "gate_result", "variant_id": self.variant_id,
                "passes": dict(self.passes), "metrics": dict(self.metrics),
                "tripwires": dict(self.tripwires), "notes": self.notes}

    @classmethod
    def from_dict(cls, d: dict) -> "OracleGateResult":
        return cls(
            variant_id=d["variant_id"],
            passes=dict(d["passes"]),
            metrics=dict(d["metrics"]),
            tripwires=dict(d.get("tripwires") or {}),
            notes=str(d.get("notes") or ""),
        )


class Oracle(ABC):
    """The generalization seam. A project implements ``run_gates``; the rest is optional."""

    def incumbent(self) -> "dict | None":
        """The current best variant's metrics to beat (``None`` on a fresh project).

        Optional: the engine never calls it. Override only if something consumes it."""
        return None

    @abstractmethod
    def run_gates(self, variant_path: str, cfg: dict) -> OracleGateResult:
        """Compute the deterministic passes and tripwire flags for a variant.

        This is the ONLY method a project oracle must implement."""

    def proposal_space(self) -> "list[str]":
        """The allowed mechanism cells. When non-empty, the maker picks ``cell`` from it,
        which keeps the registry's exact-match dedup meaningful."""
        return []

    def soft_passes(self) -> "list[str]":
        """Names of the checker-judged (soft) passes, in target-selection order.

        The decision reads these names from here, so the core never hard-codes them. The
        default declares none: the total is then the oracle passes alone."""
        return []


def load_oracle(spec: str, root: "Path | str"):
    """Resolve an ``"module.path:ClassName"`` spec to an oracle instance.

    - ``core.kernel.*`` specs import from the shipped kernel package (for example the toy oracle).
    - Anything else maps the module dots to a file under ``root`` (with a ``harness/``
      fallback for stamped layouts) and loads it with importlib.
    """
    module, _, cls_name = str(spec).partition(":")
    if not module or not cls_name:
        raise OracleLoadError(f"oracle spec must be 'module.path:ClassName', got {spec!r}")

    if module.startswith("core.kernel."):
        mod = importlib.import_module(module)
    else:
        rel = Path(*module.split(".")).with_suffix(".py")
        variants = [Path(root) / rel, Path(root) / "harness" / rel]
        path = next((c for c in variants if c.is_file()), None)
        if path is None:
            tried = ", ".join(str(c) for c in variants)
            raise OracleLoadError(f"oracle module file not found for {spec!r} (tried: {tried})")
        name = "_kernel_oracle_" + module.replace(".", "_")
        loader_spec = importlib.util.spec_from_file_location(name, path)
        if loader_spec is None or loader_spec.loader is None:
            raise OracleLoadError(f"cannot load oracle module from {path}")
        mod = importlib.util.module_from_spec(loader_spec)
        loader_spec.loader.exec_module(mod)

    cls = getattr(mod, cls_name, None)
    if cls is None:
        raise OracleLoadError(f"class {cls_name!r} not found in oracle module {module!r}")
    return cls()
