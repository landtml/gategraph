"""SMOKE, a fast, cheap dry-run that catches a maker's bugs *before* an oracle run.

The maker has **no Bash** (it can't be trusted with arbitrary execution in a scoped sandbox),
so it cannot run its own variant, a trivial ``SyntaxError`` would otherwise burn a full
oracle run and, worse, risk being recorded as a variant REJECT (a false dead-end). SMOKE
closes that gap. The generic default is deliberately shallow (the variant file exists, is
non-empty, and, if Python, imports cleanly); a project oracle that can do better exposes an
optional ``smoke(variant_path) -> SmokeResult`` method, which wins.

On failure the traceback is handed to :class:`SmokeError`, so the driver can feed the exact
error back to a fresh maker for a bounded **repair** retry. A smoke bug is a *variant*
strike but explicitly **not** a dead-end cell, a bug is not a falsified mechanism.
The default check stays domain-agnostic on purpose: anything more specific, such as a
synthetic-data dry-run, belongs in a project oracle's own ``smoke`` method, not here.
"""
from __future__ import annotations

import importlib.util
import traceback
from dataclasses import dataclass
from pathlib import Path

from core.loop.errors import SmokeError


@dataclass
class SmokeResult:
    ok: bool
    detail: str = ""
    traceback_text: str = ""


def _load_module(path: "str | Path"):
    """Import a variant module from a file path under a unique name (no sys.modules collision)."""
    p = Path(path)
    name = f"smoke_{p.parent.name}_{p.stem}"
    spec = importlib.util.spec_from_file_location(name, str(p))
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load module spec from {p}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def smoke_check(variant_path: "str | Path", oracle=None) -> SmokeResult:
    """Cheap sanity on the variant. Never raises, returns a result.

    The oracle's optional ``smoke`` method overrides the generic default entirely."""
    custom = getattr(oracle, "smoke", None) if oracle is not None else None
    if callable(custom):
        return custom(variant_path)

    p = Path(variant_path)
    if not p.is_file():
        return SmokeResult(False, f"variant file missing: {p}")
    if p.stat().st_size == 0:
        return SmokeResult(False, f"variant file is empty: {p}")
    if p.suffix == ".py":
        try:
            _load_module(p)
        except Exception:  # noqa: BLE001, any import-time failure is a variant bug to repair
            return SmokeResult(False, "import failed", traceback.format_exc())
        return SmokeResult(True, "variant imports cleanly")
    return SmokeResult(True, "variant file present")


def smoke_or_raise(variant_path: "str | Path", oracle=None) -> SmokeResult:
    """Run :func:`smoke_check`; raise :class:`SmokeError` (carrying the traceback) on failure.

    This is the driver's seam: the raised ``SmokeError`` routes as a variant strike
    (errors.py) and its ``traceback_text`` becomes the repair feedback for the next maker."""
    result = smoke_check(variant_path, oracle)
    if not result.ok:
        raise SmokeError(result.detail, traceback_text=result.traceback_text or result.detail)
    return result
