"""Loop run-manifest, version stamp + protected-file integrity.

The harness itself can drift (someone edits an agent prompt, a gate threshold, or the oracle
mid-run). To make that structurally visible we checksum the methodology-critical files at run
start and stamp ``harness_version`` + ``config_hash`` + ``manifest_fingerprint`` into every
receipt. Every result is then traceable to the exact methodology version that produced it, and
a mid-run change to a protected file changes the fingerprint so the driver halts with a
receipt instead of silently drifting.

Distinct from ``core.manifest`` (the per-unit evidence chain), this one fingerprints the
METHODOLOGY tree. Kept dependency-free and deterministic: same tree in → same fingerprint out.
Paths to checksum are read from ``PROTECTED_PATHS`` below rather than hard-coded, so a
fork of this harness can point the fingerprint at its own methodology tree.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

# Methodology-critical paths, relative to the REPO root. Missing entries are skipped silently
# (e.g. a project without a custom oracle); the manifest simply grows to cover them.
PROTECTED_PATHS = (
    "harness/adapter",
    "harness/core/loop",
)

_HASHABLE_SUFFIXES = (".py", ".yaml", ".yml", ".md")

CONFIG_REL = "harness/adapter/adapter.yaml"


def _iter_protected_files(root: str):
    """Yield ``(relpath, fullpath)`` for every protected file that exists under ``root``."""
    for rel in PROTECTED_PATHS:
        p = os.path.join(root, rel)
        if os.path.isfile(p):
            yield rel, p
        elif os.path.isdir(p):
            for dirpath, _dirs, files in os.walk(p):
                if "__pycache__" in dirpath.split(os.sep):
                    continue
                for fn in sorted(files):
                    if fn.endswith(_HASHABLE_SUFFIXES):
                        full = os.path.join(dirpath, fn)
                        yield os.path.relpath(full, root).replace(os.sep, "/"), full


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def harness_version(root: "str | os.PathLike") -> str:
    """The stamped core version from ``harness/.fh/MANIFEST.json`` (``dev`` in the template)."""
    try:
        data = json.loads((Path(root) / "harness" / ".fh" / "MANIFEST.json")
                          .read_text(encoding="utf-8"))
        return str(data.get("version", "dev"))
    except (OSError, ValueError):
        return "dev"


def file_checksums(root: str) -> "dict[str, str]":
    """Map of ``relpath -> sha256`` for all protected files, sorted by path (deterministic)."""
    return {rel: _sha256(full) for rel, full in sorted(_iter_protected_files(str(root)))}


def config_hash(root: str) -> str:
    """SHA-256 of adapter.yaml, the single most-stamped provenance value."""
    p = os.path.join(str(root), CONFIG_REL)
    return _sha256(p) if os.path.isfile(p) else hashlib.sha256(b"").hexdigest()


@dataclass(frozen=True)
class Manifest:
    harness_version: str
    config_hash: str
    files: "dict[str, str]"

    @property
    def fingerprint(self) -> str:
        """One hash over version + every (relpath, sha256) pair, the manifest identity."""
        h = hashlib.sha256()
        h.update(self.harness_version.encode())
        for rel in sorted(self.files):
            h.update(rel.encode())
            h.update(self.files[rel].encode())
        return h.hexdigest()

    def stamp(self) -> "dict[str, str]":
        """The compact provenance dict attached to every receipt."""
        return {
            "harness_version": self.harness_version,
            "config_hash": self.config_hash,
            "manifest_fingerprint": self.fingerprint,
        }

    def to_dict(self) -> dict:
        return {
            "harness_version": self.harness_version,
            "config_hash": self.config_hash,
            "manifest_fingerprint": self.fingerprint,
            "files": dict(self.files),
        }


def compute_manifest(root: "str | os.PathLike") -> Manifest:
    """Checksum the protected tree now and return an immutable :class:`Manifest`."""
    root = str(root)
    return Manifest(harness_version(root), config_hash(root), file_checksums(root))


def diff(old: Manifest, new: Manifest) -> dict:
    """Which protected files changed between two manifests (for mid-run drift detection)."""
    old_f, new_f = old.files, new.files
    old_keys, new_keys = set(old_f), set(new_f)
    return {
        "added": sorted(new_keys - old_keys),
        "removed": sorted(old_keys - new_keys),
        "modified": sorted(k for k in old_keys & new_keys if old_f[k] != new_f[k]),
    }
