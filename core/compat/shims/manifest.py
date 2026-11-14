"""Version-1 ``manifest``: ``load_for``, ``Manifest``, ``ManifestError``, ``sha256_file``
(plan 1.18; brief M3; bug B01 for migrated projects).

Reads go to the projection the runner wrote (``manifest.yaml`` under the unit, schema 2, or the
imported schema-1 record). ``Manifest.set_gate`` does not write a file. It refuses any caller that
is not the gate's registered legacy producer, then asks the runner to attest the gate (see
``shims/gate.py``). The gate passes only if the runner seals it, and the view is reloaded to show
the result. ``Manifest.save`` persists nothing, because the runner writes the projection after
every run; it stays so that version-1 scripts keep running.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from core.compat.shims import deprecated
from core.compat.shims import gate as _gate
from core.kernel import ledger as _ledger
from core.kernel import paths as _kpaths
from core.kernel import projection as _projection


class ManifestError(Exception):
    """Raised on a refusal, an unknown gate, or a manifest that cannot be read."""


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class Manifest:
    """The version-1 view of one unit. Built by ``load_for``; reads the projection."""

    def __init__(self, root, cfg, slug: str):
        self.root = Path(root)
        self.cfg = cfg
        self.slug = slug
        self.unit_dir = _kpaths.unit_path(self.root, cfg.unit_dir, slug)
        self._reload()

    def _reload(self) -> None:
        try:
            projected = _projection.read_manifest(self.unit_dir)
        except _projection.ManifestError as exc:
            raise ManifestError(str(exc)) from exc
        self.kind = projected.kind
        self.data = projected.data

    # --- accessors ---
    @property
    def gates_order(self) -> list:
        return list(self.cfg.gates)

    @property
    def status(self) -> str:
        return str(self.data.get("status", "in_progress"))

    @property
    def title(self) -> str:
        return str(self.data.get("project") or self.cfg.project)

    def _effective_names(self) -> list:
        if self.kind == "current":
            return [g for g in (self.data.get("effective") or []) if g in self.gates_order]
        # Schema-1 record imported at migration: the gates recorded, in order, up to the first gap.
        recorded = self.data.get("gates") or {}
        names = []
        for gate in self.gates_order:
            if gate not in recorded:
                break
            names.append(gate)
        return names

    def effective_gates(self) -> dict:
        """Gates effectively passed, in gate order, each mapped to its recorded record."""
        table = self.data.get("gates") or {}
        return {g: (table.get(g) or {}) for g in self._effective_names()}

    def gate_passed(self, name: str) -> bool:
        return name in self._effective_names()

    def next_gate(self):
        done = set(self._effective_names())
        return next((g for g in self.gates_order if g not in done), None)

    @property
    def complete(self) -> bool:
        return bool(self.gates_order) and self.gate_passed(self.gates_order[-1])

    def stale_gates(self) -> list:
        return list(self.data.get("stale") or [])

    # --- writes ---
    def set_gate(self, name: str, **evidence) -> None:
        """Ask the runner to attest ``name``. Raises ManifestError unless the runner seals it."""
        deprecated("Manifest.set_gate", "the gate command and its check", self.root)
        if name not in self.gates_order:
            raise ManifestError(f"unknown gate {name!r}")
        command = _gate.legacy_producer(self.cfg.kernel, name)
        if not _gate.is_caller(self.root, command):
            self._refuse(name, command)
        if _gate.inside_attestation(self.root, self.slug, name):
            return  # the runner is running this gate's check; its exit code decides the seal
        idx = self.gates_order.index(name)
        if idx > 0 and not self.gate_passed(self.gates_order[idx - 1]):
            raise ManifestError(f"cannot set gate {name!r}: predecessor "
                                f"{self.gates_order[idx - 1]!r} not effectively passed")
        if self.gate_passed(name):
            return
        code, message = _gate.submit(self.root, self.slug, name)
        self._reload()
        if self.gate_passed(name):
            return
        raise ManifestError(f"gate {name!r} was not sealed for {self.slug} "
                            f"(runner exit {code}: {message or 'no message'})")

    def _refuse(self, name: str, command: str | None) -> None:
        caller = sys.argv[0] if sys.argv else "-c"
        _ledger.record(self.root, "shim-refused", gate=name, unit=self.slug, caller=caller)
        if command is None:
            raise ManifestError(f"refused: gate {name!r} has no registered legacy producer; "
                                f"only a gate's own command may attest it (bug B01)")
        script = _gate.producer_script(self.root, command)
        raise ManifestError(f"refused: set_gate({name!r}) was called from {caller}, which is "
                            f"not the registered legacy producer {script}; only that command "
                            f"may attest the gate (bug B01)")

    def save(self, path=None) -> None:
        """Nothing to persist: the runner writes the projection after every run."""
        deprecated("Manifest.save", "the runner's projection (written after each run)", self.root)


def load_for(root, cfg, slug: str) -> Manifest:
    """The version-1 manifest of a unit, read through the projection."""
    deprecated("manifest.load_for", "core.kernel.projection.read_manifest", root)
    return Manifest(root, cfg, slug)
