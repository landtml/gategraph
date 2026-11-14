#!/usr/bin/env python3
"""policy_scan.py: the deny policies of harness/adapter/adapter.yaml over the units.

  python3 harness/adapter/checks/policy_scan.py --unit <slug>   scan one unit (a gate's check)
  python3 harness/adapter/checks/policy_scan.py                 scan every unit (make check)

Exit 0 when no fatal finding is found. Exit 1 otherwise, one line per finding; warnings print
and never fail. A unit is a directory under unit_dir that holds a manifest.yaml. Every file in
a unit is scanned through the artifact readers of core/kernel/extract.py.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

HARNESS = pathlib.Path(__file__).resolve().parents[2]  # the directory that holds `core/`
ROOT = HARNESS.parent                                  # the project root
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))

from core.kernel import config, paths, policy, projection  # noqa: E402
from core.kernel.types import Severity  # noqa: E402

# The gate's own projections (manifest.yaml, evidence.jsonl at the unit root) hold digests and
# ids written from the chain. They are not authored content, so they are not scanned.
PROJECTIONS = frozenset({projection.MANIFEST, projection.EVIDENCE})


def unit_dirs(cfg, slug: str | None) -> list[pathlib.Path]:
    if slug is not None:
        if not paths.valid_slug(slug):
            sys.exit(f"policy_scan: invalid unit slug {slug!r}")
        unit = paths.unit_path(ROOT, cfg.unit_dir, slug)
        if not unit.is_dir():
            sys.exit(f"policy_scan: no unit {slug!r} under {cfg.unit_dir}/")
        return [unit]
    base = ROOT / cfg.unit_dir
    if not base.is_dir():
        return []
    return [d for d in sorted(base.iterdir()) if d.is_dir() and (d / "manifest.yaml").is_file()]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--unit", default=None, help="scan this unit only (default: every unit)")
    args = parser.parse_args(argv)
    try:
        cfg = config.load(ROOT)
    except config.ConfigError as exc:
        print(f"policy_scan: {exc}", file=sys.stderr)
        return 1
    policies = policy.compile_policies(cfg)
    units = unit_dirs(cfg, args.unit)
    fatal = files = 0
    for unit in units:
        for path in sorted(p for p in unit.rglob("*") if p.is_file()):
            if "__pycache__" in path.parts:
                continue
            if path.parent == unit and path.name in PROJECTIONS:
                continue
            files += 1
            result = policy.scan_path(path, ROOT, policies)
            for note in result.notes:
                print(f"note: {note['path']}: not scanned ({note['reason']})")
            for finding in result.findings:
                print(f"{finding.path}:{finding.line}: {finding.rule} "
                      f"({finding.severity.value}): {finding.message}")
                if finding.severity == Severity.FATAL:
                    fatal += 1
    if fatal:
        print(f"policy: FAIL, {fatal} fatal finding(s) in {len(units)} unit(s)")
        return 1
    print(f"policy: OK ({len(units)} unit(s), {files} file(s) scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
