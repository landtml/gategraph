"""Offline verification of one unit in five steps (plan 1.8, bug B05 detection half).

``verify_unit`` proves a unit in the order of section 1.8 and stops at the first
failing step, which it names:

1. Walk ``evidence.jsonl``: every digest and every ``prev`` link.
2. Compare the manifest's head and gate table with the chain.
3. Load the checkpoint named in the manifest and compare its chain, record by
   record, and its status, with the files. Needs ``checkpoint_reader``; without
   one the step is ``not checked (no database)``.
4. Surfaces: every gate the chain has passed must have a surface that loads and
   verifies, and the manifest's effective gates must match what the files on disk
   say now. A surface that cannot be found is ``surface missing``; one that does
   not hash to its digest is ``surface corrupt``. Neither is reported as stale.
   The stored map in the state directory is used when the file exists; otherwise
   the ``surface`` key of ``evidence.jsonl`` is used, if its digest matches.
5. With ``mac_key``, verify each record's ``mac``; without a key it is not checked.

Steps 1, 2, 4 and 5 use only the kernel and run offline on a clone. A schema-1
manifest is reported as ``legacy``, which is never ``ok``.

Standard library and PyYAML only. The kernel never imports the graph layer: the
checkpoint is read through the ``checkpoint_reader`` callable.
"""
from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from core.kernel import paths
from core.kernel import surface as _surface
from core.kernel.evidence import SURFACE_KEY, ChainError, parse_record, verify_chain
from core.kernel.projection import ManifestError, read_evidence, read_manifest, summarize
from core.kernel.types import RecordKind, Surface

PASS = "pass"
FAIL = "fail"
NOT_RUN = "not run"
NOT_CHECKED_DB = "not checked (no database)"
NOT_CHECKED_KEY = "not checked (no key)"
NOT_CHECKED_LEGACY = "not checked (legacy manifest)"
STEP_NAMES = {1: "evidence chain", 2: "manifest head and gates", 3: "checkpoint",
              4: "surfaces", 5: "mac"}
_PASSING = (RecordKind.GATE, RecordKind.APPROVAL, RecordKind.IMPORT)


@dataclasses.dataclass(frozen=True)
class StepResult:
    step: int
    name: str
    status: str
    detail: str = ""


@dataclasses.dataclass(frozen=True)
class VerifyReport:
    unit_dir: str
    legacy: bool
    steps: tuple[StepResult, ...]

    @property
    def failed_step(self) -> int | None:
        for step in self.steps:
            if step.status == FAIL:
                return step.step
        return None

    @property
    def failed_detail(self) -> str:
        for step in self.steps:
            if step.status == FAIL:
                return step.detail
        return ""

    @property
    def ok(self) -> bool:
        return not self.legacy and self.failed_step is None

    def summary(self) -> str:
        """One line per step, for a command-line report."""
        lines = []
        for step in self.steps:
            text = f"step {step.step} {step.name}: {step.status}"
            if step.detail:
                text += f" ({step.detail})"
            lines.append(text)
        verdict = "OK" if self.ok else ("LEGACY" if self.legacy else "FAILED")
        lines.append(verdict)
        return "\n".join(lines)


class _Failed(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def _legacy_report(unit: Path) -> VerifyReport:
    steps = tuple(StepResult(n, STEP_NAMES[n], NOT_CHECKED_LEGACY,
                             "schema 1 manifest: no evidence chain to verify")
                  for n in sorted(STEP_NAMES))
    return VerifyReport(str(unit), True, steps)


def _step_chain(unit: Path, state: dict) -> tuple[str, str]:
    try:
        lines = read_evidence(unit)
        records = []
        copies: dict[str, Any] = {}
        for seq, obj in enumerate(lines):
            rec = parse_record(obj, seq)
            records.append(rec)
            if SURFACE_KEY in obj:
                copies[rec.surface_digest] = obj[SURFACE_KEY]
        verify_chain(records)
    except ChainError as exc:
        raise _Failed(str(exc)) from None
    state["records"] = records
    state["copies"] = copies
    return PASS, f"{len(records)} record(s)"


def _step_head(unit: Path, manifest, problem: str | None, state: dict) -> tuple[str, str]:
    if manifest is None:
        raise _Failed(problem or "manifest.yaml is missing")
    head, table = summarize(state["records"])
    claimed_head = manifest.data.get("head")
    if claimed_head != head:
        raise _Failed(f"manifest head {claimed_head!r} does not match the evidence head {head!r}")
    claimed_table = manifest.data.get("gates")
    if not isinstance(claimed_table, dict):
        raise _Failed("manifest gate table is malformed")
    if claimed_table != table:
        differing = sorted({gate for gate in set(table) | set(claimed_table)
                            if table.get(gate) != claimed_table.get(gate)})
        raise _Failed(f"manifest gate table does not match the chain: {', '.join(differing)}")
    return PASS, f"head seq {head['seq'] if head else 'none'}"


def _step_checkpoint(manifest, reader, state: dict) -> tuple[str, str]:
    if reader is None:
        return NOT_CHECKED_DB, ""
    checkpoint_id = manifest.data.get("checkpoint_id")
    if not checkpoint_id:
        raise _Failed("manifest names no checkpoint")
    try:
        found = reader(manifest.data["thread_id"], checkpoint_id)
    except Exception as exc:  # the reader is injected; any failure is a finding
        raise _Failed(f"checkpoint {checkpoint_id} could not be read: {exc}") from None
    if found is None:
        raise _Failed(f"checkpoint {checkpoint_id} not found")
    if not isinstance(found, Mapping) or "evidence" not in found:
        raise _Failed(f"checkpoint {checkpoint_id} carries no evidence list")
    try:
        stored = [parse_record(raw, seq).to_dict() for seq, raw in enumerate(found["evidence"])]
    except (ChainError, TypeError) as exc:
        raise _Failed(f"checkpoint {checkpoint_id} evidence is malformed: {exc}") from None
    on_disk = [rec.to_dict() for rec in state["records"]]
    if len(stored) != len(on_disk):
        raise _Failed(f"checkpoint holds {len(stored)} record(s), evidence.jsonl holds {len(on_disk)}")
    for seq, (a, b) in enumerate(zip(stored, on_disk)):
        if a != b:
            raise _Failed(f"record {seq} differs between checkpoint {checkpoint_id} and evidence.jsonl")
    if "status" in found and found["status"] != manifest.data.get("status"):
        raise _Failed(f"manifest status {manifest.data.get('status')!r} differs from "
                      f"checkpoint status {found['status']!r}")
    return PASS, f"checkpoint {checkpoint_id}"


def _loader(state_dir: Path | None, copies: dict) -> Callable[[str], dict]:
    """The surface lookup for step 4: the stored map first, then the evidence copy."""
    def load(digest: str) -> dict:
        if state_dir is not None and (state_dir / "surfaces" / f"{digest}.json").exists():
            try:
                return _surface.load_surface(state_dir, digest)
            except _surface.SurfaceMissing as exc:
                raise _Failed(str(exc)) from None
        if digest in copies:
            value = copies[digest]
            if isinstance(value, dict) and Surface(files=value).digest() == digest:
                return value
            raise _Failed(f"surface corrupt: the evidence.jsonl copy for {digest} "
                          f"does not hash to its digest")
        raise _Failed(f"surface missing: {digest}")
    return load


def _step_surfaces(unit: Path, cfg, manifest, state: dict) -> tuple[str, str]:
    root = getattr(cfg, "root", None)
    state_dir = paths.state_dir(root) if root is not None else None
    load = _loader(state_dir, state["copies"])
    records = state["records"]

    latest: dict[str, Any] = {}
    for rec in records:
        if rec.kind in _PASSING and rec.gate is not None:
            latest[rec.gate] = rec
    for rec in latest.values():  # every passed gate's surface must load, stale or not
        load(rec.surface_digest)

    try:
        effective = _surface.effective_gates(records, cfg, unit, load)
        stale = _surface.stale_gates(records, cfg, unit, load)
    except _Failed:
        raise
    except (ChainError, ValueError, TypeError) as exc:
        raise _Failed(str(exc)) from None

    claimed = manifest.data.get("effective")
    if not isinstance(claimed, list):
        raise _Failed("manifest effective list is malformed")
    if claimed != effective:
        changed = [gate for gate in claimed if gate in stale]
        if changed:
            raise _Failed("gates changed since their record (stale): " + ", ".join(changed))
        raise _Failed(f"manifest says effective {claimed}, the files say {effective}")
    return PASS, f"effective: {', '.join(effective) or 'none'}"


def _step_mac(records: list, mac_key) -> tuple[str, str]:
    if mac_key is None:
        return NOT_CHECKED_KEY, ""
    try:
        verify_chain(records, mac_key=mac_key)
    except ChainError as exc:
        raise _Failed(str(exc)) from None
    return PASS, "every mac verifies"


def verify_unit(unit_dir, cfg, checkpoint_reader=None, *, mac_key=None) -> VerifyReport:
    """Verify one unit and return a VerifyReport. Never writes to the unit or the state.

    ``cfg`` is a configuration with ``.gates`` and ``.root`` (the project root, which
    locates the state directory). ``checkpoint_reader(thread_id, checkpoint_id)``
    returns a mapping with an ``evidence`` list (and optionally ``status``), or None
    when the checkpoint does not exist. ``mac_key`` enables step 5.
    """
    unit = Path(unit_dir)
    try:
        manifest = read_manifest(unit)
        problem = None
    except ManifestError as exc:
        manifest, problem = None, str(exc)
    if manifest is not None and manifest.kind == "legacy":
        return _legacy_report(unit)

    state: dict[str, Any] = {}
    plan = [
        (1, lambda: _step_chain(unit, state)),
        (2, lambda: _step_head(unit, manifest, problem, state)),
        (3, lambda: _step_checkpoint(manifest, checkpoint_reader, state)),
        (4, lambda: _step_surfaces(unit, cfg, manifest, state)),
        (5, lambda: _step_mac(state.get("records", []), mac_key)),
    ]
    results: list[StepResult] = []
    failed = False
    for number, run in plan:
        name = STEP_NAMES[number]
        if failed:
            results.append(StepResult(number, name, NOT_RUN))
            continue
        try:
            status, detail = run()
        except _Failed as exc:
            status, detail, failed = FAIL, exc.detail, True
        results.append(StepResult(number, name, status, detail))
    return VerifyReport(str(unit), False, tuple(results))


__all__ = ["VerifyReport", "StepResult", "verify_unit"]
