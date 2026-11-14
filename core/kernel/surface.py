"""Surface hashing, the surface store, and effective and stale gates (plan 1.7, 1.8).

A gate's surface is the set of unit files it covers, as {relative path: sha256}.
A gate G is effective only when all of these hold:

1. its latest passing record's surface matches the files on disk exactly (no
   file changed, added or deleted inside the surface);
2. every gate in G.needs is effective;
3. for each gate N in G.needs, the latest record of N has a lower ``seq`` than
   the latest record of G. A needed gate re-earned after G invalidates G, even
   when no file changed (the legacy set_gate behaviour, core/manifest.py lines
   129 to 174, and the reset_invalidates_later scenario).

The default ``needs`` is the previous gate, as the config resolves it. These are
pure functions of the chain, the files and the surface store.

Surface rules: ``surface`` include and exclude globs when the gate sets them,
otherwise the whole unit. Always excluded: bookkeeping files (manifest.yaml,
evidence.jsonl) and any file that a LATER gate in the list declares in
``produces``. The record stores only the surface digest; the map lives in
``<state_dir>/surfaces/<digest>.json``.

Standard library only.
"""
from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable

from core.kernel.evidence import ChainError, is_digest, parse_record, verify_chain
from core.kernel.types import GateSpec, RecordKind, Surface, canonical_json

BOOKKEEPING = frozenset({"manifest.yaml", "evidence.jsonl"})
DEFAULT_INCLUDE = ("**",)
_PASSING_KINDS = (RecordKind.GATE, RecordKind.APPROVAL, RecordKind.IMPORT)


class SurfaceMissing(Exception):
    """A surface file is absent, unreadable, not JSON, or does not hash to its digest."""


# --------------------------------------------------------------------------
# Paths and globs
# --------------------------------------------------------------------------

@functools.lru_cache(maxsize=256)
def _glob(pattern: str) -> re.Pattern:
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out))


def _matches(name: str, patterns: Iterable[str]) -> bool:
    return any(_glob(pattern).fullmatch(name) is not None for pattern in patterns)


def _shape_ok(name: Any) -> bool:
    """A unit-relative POSIX path: no absolute start, no backslash, no dot segments."""
    if not isinstance(name, str) or not name or name.startswith("/"):
        return False
    if "\\" in name or "\x00" in name:
        return False
    return all(part not in ("", ".", "..") for part in name.split("/"))


def _check_recorded_name(name: Any, unit_dir: Path) -> str:
    """Refuse a recorded path that is not inside the unit (bug B33)."""
    if not _shape_ok(name):
        raise ChainError(f"recorded path {name!r} is not a unit-relative path")
    root = unit_dir.resolve()
    try:
        (unit_dir / name).resolve().relative_to(root)
    except ValueError:
        raise ChainError(f"recorded path {name!r} resolves outside the unit") from None
    return name


def _unit_files(unit_dir: Path) -> dict[str, Path]:
    """Every unit file except bookkeeping, keyed by unit-relative POSIX name."""
    if not unit_dir.is_dir():
        return {}
    real = unit_dir.resolve()
    found: dict[str, Path] = {}
    for path in unit_dir.rglob("*"):
        if not path.is_file() or path.name in BOOKKEEPING:
            continue
        name = path.relative_to(unit_dir).as_posix()
        try:
            path.resolve().relative_to(real)
        except ValueError:
            raise ChainError(f"file {name!r} resolves outside the unit") from None
        found[name] = path
    return found


def _sha256_file(path: Path) -> str:
    with open(path, "rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def _gate_list(cfg: Any) -> list[GateSpec]:
    gates = cfg if isinstance(cfg, (list, tuple)) else getattr(cfg, "gates", None)
    if gates is None:
        raise TypeError("cfg must be a list of GateSpec or expose .gates")
    return list(gates)


# --------------------------------------------------------------------------
# Surface hashing
# --------------------------------------------------------------------------

def hash_surface(unit_dir, gate_id: str, gates) -> Surface:
    """The surface of ``gate_id`` as the files on disk are now.

    ``gates`` is the ordered gate list; "later" means later in that list.
    Returns Surface({relative path: sha256}). Raises ChainError for a file that
    resolves outside the unit (a symlink escape).
    """
    gates = _gate_list(gates)
    ids = [gate.id for gate in gates]
    if gate_id not in ids:
        raise ValueError(f"unknown gate {gate_id!r}")
    index = ids.index(gate_id)
    spec = gates[index]
    if spec.surface is None:
        include, exclude = DEFAULT_INCLUDE, ()
    else:
        include, exclude = spec.surface.include, spec.surface.exclude
    later_produces = [pattern for gate in gates[index + 1:] for pattern in gate.produces]
    files = {}
    for name, path in _unit_files(Path(unit_dir)).items():
        if _matches(name, later_produces) or _matches(name, exclude):
            continue
        if not _matches(name, include):
            continue
        files[name] = _sha256_file(path)
    return Surface(files=files)


# --------------------------------------------------------------------------
# Surface store: <state_dir>/surfaces/<digest>.json
# --------------------------------------------------------------------------

def store_surface(state_dir, surface: Surface) -> str:
    """Write the surface once, content-addressed, and return its digest.

    An existing file is never replaced, so storing the same surface twice writes
    nothing new. The file is written to a temporary name and linked into place,
    so a reader never sees a partial file.
    """
    if not isinstance(surface, Surface):
        raise TypeError("store_surface takes a Surface")
    for name, digest in surface.files.items():
        if not _shape_ok(name):
            raise ValueError(f"surface path {name!r} is not a unit-relative path")
        if not is_digest(digest):
            raise ValueError(f"surface entry {name!r} is not a sha256 digest")
    digest = surface.digest()
    directory = Path(state_dir) / "surfaces"
    target = directory / f"{digest}.json"
    if target.exists():
        return digest
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".surface-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(canonical_json(surface.files))
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, target)
        except FileExistsError:
            pass
        except OSError:
            if not target.exists():
                os.replace(tmp, target)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
    return digest


def load_surface(state_dir, digest: str) -> dict:
    """The surface map stored under ``digest``. Raises SurfaceMissing when the file
    is absent, unreadable, or its content does not hash to ``digest``."""
    if not is_digest(digest):
        raise SurfaceMissing(f"surface digest {digest!r} is not a sha256 digest")
    path = Path(state_dir) / "surfaces" / f"{digest}.json"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SurfaceMissing(f"surface missing: {digest}") from None
    except (OSError, UnicodeDecodeError) as exc:
        raise SurfaceMissing(f"surface unreadable: {digest}: {exc}") from exc
    try:
        files = json.loads(text)
    except ValueError:
        raise SurfaceMissing(f"surface corrupt: {digest} is not JSON") from None
    if not isinstance(files, dict) or Surface(files=files).digest() != digest:
        raise SurfaceMissing(f"surface corrupt: content does not hash to {digest}")
    return files


# --------------------------------------------------------------------------
# Effective and stale gates
# --------------------------------------------------------------------------

def _evaluate(chain, cfg, unit_dir, surfaces: Callable[[str], dict]) -> tuple[list, list]:
    gates = _gate_list(cfg)
    by_id = {gate.id: gate for gate in gates}
    if len(by_id) != len(gates):
        raise ValueError("duplicate gate id")
    verify_chain(chain)
    unit = Path(unit_dir)

    latest = {}
    latest_seq = {}
    for seq, raw in enumerate(chain):
        rec = parse_record(raw, seq)
        if rec.kind in _PASSING_KINDS and rec.gate is not None:
            latest[rec.gate] = rec
            latest_seq[rec.gate] = seq

    def surface_matches(gate_id: str, rec) -> bool:
        recorded = surfaces(rec.surface_digest)
        if not isinstance(recorded, dict):
            raise ChainError(f"surface for {gate_id!r} is not a mapping")
        for name, digest in recorded.items():
            _check_recorded_name(name, unit)
            if not is_digest(digest):
                raise ChainError(f"surface entry {name!r} for {gate_id!r} is malformed")
        return dict(recorded) == hash_surface(unit, gate_id, gates).files

    memo: dict[str, bool] = {}

    def effective(gate_id: str, trail: frozenset) -> bool:
        if gate_id in memo:
            return memo[gate_id]
        if gate_id in trail:
            raise ValueError(f"needs cycle through {gate_id!r}")
        spec = by_id[gate_id]
        for need in spec.needs:
            if need not in by_id:
                raise ValueError(f"gate {gate_id!r} needs unknown gate {need!r}")
        rec = latest.get(gate_id)
        ok = rec is not None and all(effective(need, trail | {gate_id}) for need in spec.needs)
        # A needed gate re-earned after this gate invalidates it (reset rule).
        if ok:
            ok = all(need in latest and latest_seq[need] < latest_seq[gate_id]
                     for need in spec.needs)
        if ok:
            ok = surface_matches(gate_id, rec)
        memo[gate_id] = ok
        return ok

    eff = [gate.id for gate in gates if effective(gate.id, frozenset())]
    stale = [gate.id for gate in gates if gate.id in latest and gate.id not in eff]
    return eff, stale


def effective_gates(chain, cfg, unit_dir, surfaces: Callable[[str], dict]) -> list:
    """Gate ids, in gate order, whose latest passing record still matches the unit.

    Pure in the chain, the files on disk and ``surfaces`` (normally
    ``functools.partial(load_surface, state_dir)``). Raises ChainError for a broken
    chain or a recorded path outside the unit, and SurfaceMissing when a surface
    cannot be loaded. Neither is reported as stale.
    """
    return _evaluate(chain, cfg, unit_dir, surfaces)[0]


def stale_gates(chain, cfg, unit_dir, surfaces: Callable[[str], dict]) -> list:
    """Gate ids that have a passing record but are not effective now."""
    return _evaluate(chain, cfg, unit_dir, surfaces)[1]


__all__ = ["BOOKKEEPING", "SurfaceMissing", "effective_gates", "hash_surface",
           "load_surface", "stale_gates", "store_surface"]
