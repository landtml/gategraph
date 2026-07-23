"""manifest.py, the per-unit evidence chain.

A manifest.yaml records the adapter-declared ordered gates. A gate may only be
set if its predecessor is effectively passed. Gates that carry sha256 maps
(cfg.workflow.hashed_gates) are auto-invalidated when any recorded file no
longer matches its hash, or when the unit holds a file the record never
covered, editing OR adding an artifact after verification mechanically
reopens the gate and everything downstream (the staleness model; F36).

Writes are atomic (tmp + os.replace); loads validate the schema and raise
ManifestError on corruption, which `gate.py repair` can rebuild.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from datetime import datetime
from pathlib import Path

try:
    import yaml
except ImportError as exc:  # pragma: no cover
    raise SystemExit("PyYAML is required (pip install pyyaml)") from exc

STATUSES = {"in_progress", "paused", "abandoned", "escalated"}

# The unit's hash surface: every file under the unit dir except the evidence
# chain's own bookkeeping. lint.unit_files and _hash_ok MUST share this rule,
# or a file one side sees and the other ignores becomes a staleness blind
# spot. An adapter's declared reviewer evidence_file joins the set via
# load_for, it is gate evidence, anchored by its own gate's record.
UNIT_EXCLUDE = {"manifest.yaml"}


def unit_file_names(unit_dir: Path, exclude: set = frozenset(UNIT_EXCLUDE)) -> set:
    """Unit-relative POSIX names of the files a hashed gate must cover."""
    unit_dir = Path(unit_dir)
    if not unit_dir.exists():
        return set()
    return {p.relative_to(unit_dir).as_posix() for p in unit_dir.rglob("*")
            if p.is_file() and p.name not in exclude}


class ManifestError(Exception):
    """Raised on schema violation or corrupt manifest data."""


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class Manifest:
    def __init__(self, data: dict, unit_dir: Path, gates: list, hashed_gates: list,
                 exclude: set | None = None):
        self.data = data
        self.unit_dir = Path(unit_dir)
        self.gates_order = list(gates)
        self.hashed = set(hashed_gates)
        self.exclude = set(exclude) if exclude is not None else set(UNIT_EXCLUDE)

    # --- construction ---
    @classmethod
    def new(cls, slug: str, unit_dir: Path, gates: list, hashed_gates: list,
            title: str = "", meta: dict | None = None,
            exclude: set | None = None) -> "Manifest":
        data = {
            "slug": slug,
            "title": title,
            "created": _now(),
            "status": "in_progress",
            "meta": meta or {},
            "gates": {},
        }
        return cls(data, unit_dir, gates, hashed_gates, exclude=exclude)

    @classmethod
    def load(cls, path: Path, gates: list, hashed_gates: list,
             exclude: set | None = None) -> "Manifest":
        path = Path(path)
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            raise ManifestError(f"manifest is not valid YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise ManifestError("manifest root is not a mapping")
        for key in ("slug", "status", "gates"):
            if key not in data:
                raise ManifestError(f"manifest missing required key: {key!r}")
        if not isinstance(data["gates"], dict):
            raise ManifestError("manifest 'gates' is not a mapping")
        if data["status"] not in STATUSES:
            raise ManifestError(f"manifest 'status' invalid: {data['status']!r}")
        return cls(data, path.parent, gates, hashed_gates, exclude=exclude)

    # --- accessors ---
    @property
    def slug(self) -> str:
        return self.data["slug"]

    @property
    def title(self) -> str:
        return self.data.get("title", "")

    @property
    def status(self) -> str:
        return self.data["status"]

    @status.setter
    def status(self, value: str) -> None:
        if value not in STATUSES:
            raise ManifestError(f"invalid status {value!r}")
        self.data["status"] = value

    @property
    def complete(self) -> bool:
        """True when the final gate is effectively passed (terminal success)."""
        return self.gate_passed(self.gates_order[-1])

    # --- staleness ---
    def _hash_ok(self, gate: str) -> bool:
        rec = self.data["gates"].get(gate) or {}
        recorded = rec.get("sha256")
        if recorded is None:
            return True  # gate carries no hash evidence (approval-set)
        for fname, expected in recorded.items():
            fpath = self.unit_dir / fname
            if not fpath.exists() or sha256_file(fpath) != expected:
                return False
        # F36: a file the record never covered is unexamined evidence.
        # an ADDITION reopens the gate exactly like a modification. The
        # declared-evidence exemption is mid-flow only (a later gate's
        # artifact may legitimately appear before that gate is earned);
        # the FINAL gate seals the unit, there only the manifest is exempt.
        exclude = (self.exclude if gate != self.gates_order[-1]
                   else UNIT_EXCLUDE)
        return unit_file_names(self.unit_dir, exclude) <= set(recorded)

    def effective_gates(self) -> dict:
        """Only the gates effectively passed, honoring staleness: a hashed gate
        whose files changed is dropped, and so is every gate after it."""
        effective: dict = {}
        for gate in self.gates_order:
            rec = self.data["gates"].get(gate)
            if rec is None:
                break
            if gate in self.hashed and not self._hash_ok(gate):
                break
            effective[gate] = rec
        return effective

    def gate_passed(self, name: str) -> bool:
        return name in self.effective_gates()

    def next_gate(self) -> str | None:
        eff = self.effective_gates()
        for gate in self.gates_order:
            if gate not in eff:
                return gate
        return None

    def stale_gates(self) -> list:
        """Recorded but no longer effective (files changed since)."""
        eff = self.effective_gates()
        return [g for g in self.gates_order
                if g in self.data.get("gates", {}) and g not in eff]

    def set_gate(self, name: str, **evidence) -> None:
        if name not in self.gates_order:
            raise ManifestError(f"unknown gate {name!r}")
        idx = self.gates_order.index(name)
        if idx > 0:
            predecessor = self.gates_order[idx - 1]
            if not self.gate_passed(predecessor):
                raise ManifestError(
                    f"cannot set gate {name!r}: predecessor {predecessor!r} "
                    f"not effectively passed")
        rec = {"at": _now()}
        rec.update(evidence)
        self.data.setdefault("gates", {})[name] = rec
        # Setting a gate invalidates any later gates (they must be re-earned).
        for later in self.gates_order[idx + 1:]:
            self.data["gates"].pop(later, None)

    def escalate(self, note: str) -> None:
        """Terminal 'know when you don't know' state: stuck + written note."""
        self.data["status"] = "escalated"
        self.data["escalation"] = {"at": _now(), "note": note}

    # --- persistence ---
    def to_yaml(self) -> str:
        return yaml.safe_dump(self.data, sort_keys=False, allow_unicode=True)

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".manifest-",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(self.to_yaml())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


def load_for(root: Path, cfg, slug: str) -> Manifest:
    """Load a unit's manifest with the adapter's gate chain."""
    from . import paths
    exclude = set(UNIT_EXCLUDE)
    evidence = (cfg.reviewer or {}).get("evidence_file")
    if evidence:
        exclude.add(str(evidence))
    return Manifest.load(paths.manifest_path(root, cfg, slug),
                         cfg.gates, cfg.hashed_gates, exclude=exclude)
