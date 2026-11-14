#!/usr/bin/env python3
"""verify_golden.py: the golden snapshots are frozen.

They were recorded by the legacy engine at tag legacy-0.4 and later renamed to the current
vocabulary (rule ids, case ids, paths); FROZEN.sha256 pins the renamed bytes.

The legacy engine that recorded expected/*.json is gone, so nothing is recomputed.
FROZEN.sha256 (sha256sum format, relative to this directory) pins every
expected/*.json and scenarios.yaml. This script checks each pin.

Exit 0 and print `golden: frozen, <n> snapshots intact` when every
snapshot matches its pin and nothing is missing or unpinned. Exit 1 otherwise,
naming each offending file. Fewer than 30 snapshots is also a failure.

Stdlib only. To regenerate the snapshots, check out tag legacy-0.4.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "FROZEN.sha256"
MIN_SNAPSHOTS = 30


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_pins() -> dict:
    pins = {}
    for lineno, line in enumerate(MANIFEST.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        digest, sep, name = line.partition("  ")
        if not sep or len(digest) != 64:
            raise ValueError(f"{MANIFEST.name}:{lineno}: malformed line")
        pins[name] = digest
    return pins


def _on_disk() -> set:
    names = {f"expected/{p.name}" for p in (HERE / "expected").glob("*.json")}
    if (HERE / "scenarios.yaml").is_file():
        names.add("scenarios.yaml")
    return names


def main() -> int:
    if not MANIFEST.is_file():
        print(f"golden FAIL: missing {MANIFEST.name}", file=sys.stderr)
        return 1
    pins = _read_pins()
    on_disk = _on_disk()
    bad = []
    intact = set()
    for name in sorted(set(pins) | on_disk):
        if name not in pins:
            bad.append(f"not pinned in {MANIFEST.name}: {name}")
        elif name not in on_disk:
            bad.append(f"missing: {name}")
        elif _sha256(HERE / name) != pins[name]:
            bad.append(f"changed: {name}")
        else:
            intact.add(name)
    n = sum(1 for name in intact if name.startswith("expected/"))
    if n < MIN_SNAPSHOTS:
        bad.append(f"only {n} snapshots intact (need {MIN_SNAPSHOTS})")
    if bad:
        for b in bad:
            print(f"golden FAIL: {b}", file=sys.stderr)
        return 1
    print(f"golden: frozen, {n} snapshots intact")
    return 0


if __name__ == "__main__":
    sys.exit(main())
