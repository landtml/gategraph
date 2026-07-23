"""The Oracle interface, the generalization seam (renamed from the source's ``Adapter``
to un-collide with the per-project "adapter" layer; plan §8.2).

The engine (driver, registry, gates, decision, backstops, telemetry) is project-agnostic.
Each project supplies ONE oracle implementing this contract in ``harness/adapter/oracle.py``.
The oracle owns the project-specific evaluation authority: it knows how to load the data,
what the incumbent is, and how to compute the deterministic passes and tripwire audits.
``run_gates`` is the locked truth, no LLM is involved in producing a ``GateResult``.

Optionally an oracle may define ``smoke(variant_path) -> SmokeResult`` to replace the
generic shallow smoke check with a real dry-run (see ``smoke.py``).
"""

from __future__ import annotations

import importlib
import importlib.util
from abc import ABC, abstractmethod
from pathlib import Path

from core.loop.schemas import GateResult


class OracleLoadError(Exception):
    """The ``loop.oracle`` spec could not be resolved to a class."""


class Oracle(ABC):
    def incumbent(self) -> "dict | None":
        """The current best variant's metrics to beat (``None`` on a fresh project).

        OPTIONAL: the engine itself never calls this, it exists for project checks and
        maker-prompt assembly an adapter may add. Override only if something consumes it."""
        return None

    @abstractmethod
    def run_gates(self, variant_path: str, cfg: dict) -> GateResult:
        """Compute the deterministic passes + tripwire flags for a variant.

        The oracle computes every gate metric ITSELF (so the variant can never report its
        own score) and runs its safety audits. The result is a locked ``GateResult``; config
        thresholds from ``loop.gates.metric_floors`` are applied on top by the driver.

        This is the ONLY method a project oracle must implement.
        """

    def proposal_space(self) -> "list[str]":
        """The allowed mechanism cells. When non-empty, the driver hands this to the
        gateway and the maker prompt instructs the worker to pick ``cell`` from it.
        a controlled vocabulary keeps the registry's exact-match dedup meaningful
        (free-form cell names only ever collide via the optional LLM judge)."""
        return []


def load_oracle(spec: str, root: "Path | str"):
    """Resolve a ``loop.oracle`` spec ``"module.path:ClassName"`` to an instance.

    Two resolution modes:
    - ``core.loop.*`` specs import from the shipped core package (e.g. the toy oracle).
    - anything else maps the module dots to a file under the repo root (with a ``harness/``
      fallback for stamped layouts) and loads it via importlib, no package requirements on
      the project's adapter dir.
    """
    module, _, cls_name = str(spec).partition(":")
    if not module or not cls_name:
        raise OracleLoadError(f"oracle spec must be 'module.path:ClassName', got {spec!r}")

    if module.startswith("core.loop."):
        mod = importlib.import_module(module)
    else:
        rel = Path(*module.split(".")).with_suffix(".py")
        variants = [Path(root) / rel, Path(root) / "harness" / rel]
        path = next((c for c in variants if c.is_file()), None)
        if path is None:
            tried = ", ".join(str(c) for c in variants)
            raise OracleLoadError(f"oracle module file not found for {spec!r} (tried: {tried})")
        name = "_fh_oracle_" + module.replace(".", "_")
        loader_spec = importlib.util.spec_from_file_location(name, path)
        if loader_spec is None or loader_spec.loader is None:
            raise OracleLoadError(f"cannot load oracle module from {path}")
        mod = importlib.util.module_from_spec(loader_spec)
        loader_spec.loader.exec_module(mod)

    cls = getattr(mod, cls_name, None)
    if cls is None:
        raise OracleLoadError(f"class {cls_name!r} not found in oracle module {module!r}")
    return cls()
