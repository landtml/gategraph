"""Projections (plan 1.8, 1.15): evidence.jsonl and manifest.yaml, written from the chain.

Both files are projections. They are written by the runner after a run returns,
they are never the authority, and ``verify.verify_unit`` checks them against the
chain, the files on disk and, when a reader is supplied, the checkpoint.

``evidence.jsonl`` has one canonical JSON line per record. Each line carries the
record's surface map under the ``surface`` key, which sits outside the record
digest (plan 1.8). ``manifest.yaml`` (schema 2) names the thread, the head record,
the gate table, the effective and stale gates, and the checkpoint. It holds no
timestamp of its own, so the same inputs give the same bytes.

A schema-1 manifest (the legacy core/manifest.py format) is read as a ``legacy``
projection and carries no evidence chain.

Standard library and PyYAML only.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

from core.kernel import paths
from core.kernel import surface as _surface
from core.kernel.evidence import SURFACE_KEY, ChainError, parse_record, verify_chain
from core.kernel.types import RecordKind, canonical_json

SCHEMA = 2
EVIDENCE = "evidence.jsonl"
MANIFEST = "manifest.yaml"
CURRENT_KEYS = ("unit", "thread_id", "status", "config_digest", "checkpoint_id",
                "head", "gates", "effective", "stale")
_PASSING = (RecordKind.GATE, RecordKind.APPROVAL, RecordKind.IMPORT)


class ManifestError(Exception):
    """manifest.yaml is missing, unreadable, not YAML, or not a known schema."""


@dataclass(frozen=True)
class ProjectedManifest:
    """A manifest as read. ``kind`` is ``current`` (schema 2) or ``legacy`` (schema 1)."""
    kind: str
    data: dict


def read_manifest(unit_dir) -> ProjectedManifest:
    """Read ``manifest.yaml`` of a unit. Raises ManifestError when it cannot be used."""
    path = Path(unit_dir) / MANIFEST
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ManifestError("manifest.yaml is missing") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestError(f"manifest.yaml is unreadable: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestError(f"manifest.yaml is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError("manifest.yaml root is not a mapping")
    if data.get("schema") == SCHEMA:
        missing = [key for key in CURRENT_KEYS if key not in data]
        if missing:
            raise ManifestError(f"manifest.yaml is missing key(s): {', '.join(missing)}")
        return ProjectedManifest("current", data)
    if "schema" not in data and "slug" in data and isinstance(data.get("gates"), dict):
        return ProjectedManifest("legacy", data)
    raise ManifestError("manifest.yaml has an unknown schema")


def read_evidence(unit_dir) -> list[dict]:
    """The raw lines of ``evidence.jsonl`` as dicts, the ``surface`` key included.

    A missing file is an empty chain. Raises ChainError for a line that is not a
    JSON object, naming its line number.
    """
    path = Path(unit_dir) / EVIDENCE
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ChainError(f"evidence.jsonl is unreadable: {exc}") from exc
    lines = []
    for number, line in enumerate(text.splitlines(), start=1):
        try:
            obj = json.loads(line)
        except ValueError:
            raise ChainError(f"evidence.jsonl line {number} is not JSON") from None
        if not isinstance(obj, dict):
            raise ChainError(f"evidence.jsonl line {number} is not an object")
        lines.append(obj)
    return lines


def summarize(records) -> tuple[dict | None, dict]:
    """The head and the gate table of a verified chain (EvidenceRecord list).

    The head is ``{seq, digest}`` of the last record, or None for an empty chain.
    The gate table maps each gate to its latest passing record's ``{seq, digest, at}``.
    """
    head = None
    table: dict[str, dict] = {}
    for rec in records:
        head = {"seq": rec.seq, "digest": rec.digest}
        if rec.kind in _PASSING and rec.gate is not None:
            table[rec.gate] = {"seq": rec.seq, "digest": rec.digest, "at": rec.at}
    return head, table


def _atomic_text(path: Path, text: str) -> None:
    paths.atomic_write(path, text)


def write_projections(unit_dir, cfg, chain, *, thread_id: str, status: str,
                      surfaces: Callable[[str], dict], config_digest: str,
                      checkpoint_id: str | None = None) -> dict:
    """Write ``evidence.jsonl`` and ``manifest.yaml`` for ``chain`` and return their paths.

    ``chain`` is the list of records (EvidenceRecord or mappings). ``surfaces`` maps a
    surface digest to its map, normally ``functools.partial(load_surface, state_dir)``;
    every record's surface must be loadable, or SurfaceMissing (or ChainError) is raised
    and nothing is written. ``cfg`` is a configuration with ``.gates`` (and ``.project``
    for the manifest). ``checkpoint_id`` is the checkpoint the runner read the chain from;
    None when the runner has none.

    Each file is replaced atomically. Output depends only on the arguments and the
    files on disk, so writing the same chain again gives the same bytes.
    """
    unit = Path(unit_dir)
    records = [parse_record(raw, seq) for seq, raw in enumerate(chain)]
    verify_chain(records)

    lines = []
    for rec in records:
        files = surfaces(rec.surface_digest)
        if not isinstance(files, dict):
            raise ChainError(f"surface for record {rec.seq} is not a mapping", rec.seq)
        obj = rec.to_dict()
        obj[SURFACE_KEY] = files
        lines.append(canonical_json(obj))
    evidence_text = "".join(line + "\n" for line in lines)

    effective = _surface.effective_gates(records, cfg, unit, surfaces)
    stale = _surface.stale_gates(records, cfg, unit, surfaces)
    head, table = summarize(records)
    manifest = {
        "schema": SCHEMA,
        "unit": unit.name,
        "project": getattr(cfg, "project", None),
        "thread_id": thread_id,
        "status": status,
        "config_digest": config_digest,
        "checkpoint_id": checkpoint_id,
        "head": head,
        "gates": table,
        "effective": effective,
        "stale": stale,
    }
    manifest_text = yaml.safe_dump(manifest, sort_keys=True, allow_unicode=True,
                                   default_flow_style=False)

    evidence_path = unit / EVIDENCE
    manifest_path = unit / MANIFEST
    _atomic_text(evidence_path, evidence_text)
    _atomic_text(manifest_path, manifest_text)
    return {"evidence": evidence_path, "manifest": manifest_path}


__all__ = ["CURRENT_KEYS", "EVIDENCE", "MANIFEST", "ManifestError", "ProjectedManifest",
           "read_evidence", "read_manifest", "summarize", "write_projections"]
