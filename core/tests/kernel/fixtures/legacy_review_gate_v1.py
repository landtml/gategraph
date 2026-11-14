#!/usr/bin/env python3
"""review_gate.py, the ONLY producer of this project's `reviewed` gate (§6b/§11).

`reviewed` is a tool_set gate: gate.py refuses manual approve, and this check
is the tool that earns it. It validates the unit's review.json (schema-valid,
verdict `pass`, and sha256-anchored to the CURRENT artifact), then records the
gate with a sha256 evidence map exactly like lint.py does for `linted`.

    review_gate.py --unit <slug> [--validate-only] [--root <dir>]

--validate-only is the `review-fresh` adapter check (exit 0 pass / 1 fatal):
it re-runs the same wall on every lint scan once review.json exists, so a
tampered or out-of-date review is caught even outside gate-setting.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # -> harness/
from core import config, manifest, paths  # noqa: E402

ARTIFACT = "note.md"          # this project's reviewable artifact (one per unit)
EVIDENCE = "review.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VERDICTS = {"pass", "fail", "tie"}
SEVERITIES = {"fatal", "warn", "note"}


def fail(msg: str) -> int:
    print(f"review_gate: {msg}", file=sys.stderr)
    return 1


def reviewer_cmd(cfg, slug: str) -> str:
    cmd = (cfg.reviewer.get("cross_model") or {}).get("cmd")
    if cmd:
        return str(cmd).replace("{slug}", slug)
    return (f"python3 harness/adapter/checks/cross_review.py "
            f"--artifact {cfg.unit_dir}/{slug}/{ARTIFACT} "
            f"--rubric {cfg.reviewer.get('rubric', 'harness/adapter/review_rubric.md')} "
            f"--out {cfg.unit_dir}/{slug}/{EVIDENCE}")


def validate(review: dict) -> str | None:
    """Return a problem string, or None when the review is schema-valid."""
    if not isinstance(review, dict):
        return "review.json root is not an object"
    if sorted(review) != ["artifact_sha256", "findings", "verdict"]:
        return (f"review.json keys must be exactly artifact_sha256/verdict/"
                f"findings, got {sorted(review)}")
    if not (isinstance(review["artifact_sha256"], str)
            and SHA256_RE.fullmatch(review["artifact_sha256"])):
        return "artifact_sha256 is not a 64-hex sha256"
    if review["verdict"] not in VERDICTS:
        return f"verdict must be one of {sorted(VERDICTS)}, got {review['verdict']!r}"
    if not isinstance(review["findings"], list):
        return "findings is not a list"
    for i, f in enumerate(review["findings"]):
        if (not isinstance(f, dict)
                or not isinstance(f.get("criterion"), str)
                or not isinstance(f.get("evidence"), str)
                or f.get("severity") not in SEVERITIES):
            return (f"findings[{i}] must be {{criterion, evidence, "
                    f"severity: fatal|warn|note}}")
    return None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--unit", required=True)
    p.add_argument("--validate-only", action="store_true", dest="validate_only")
    p.add_argument("--root", default=None)
    args = p.parse_args(argv)

    root = (Path(args.root).resolve() if args.root
            else Path(__file__).resolve().parents[3])
    try:
        cfg = config.load(root)
    except config.ConfigError as exc:
        return fail(str(exc))

    slug = args.unit
    unit = paths.unit_path(root, cfg, slug)
    artifact = unit / ARTIFACT
    review_path = unit / EVIDENCE

    if not review_path.is_file():
        return fail(f"no {EVIDENCE} for {slug!r}, run the blinded reviewer "
                    f"first: {reviewer_cmd(cfg, slug)}")
    try:
        review = json.loads(review_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return fail(f"{EVIDENCE} unreadable: {exc}")

    problem = validate(review)
    if problem:
        return fail(problem)
    if review["verdict"] != "pass":
        return fail(f"review verdict is {review['verdict']!r}, address the "
                    f"findings, then re-run: {reviewer_cmd(cfg, slug)}")
    if not artifact.is_file():
        return fail(f"artifact missing: {cfg.unit_dir}/{slug}/{ARTIFACT}")
    if manifest.sha256_file(artifact) != review["artifact_sha256"]:
        return fail(f"artifact changed since review, {ARTIFACT} no longer "
                    f"matches artifact_sha256; re-run: {reviewer_cmd(cfg, slug)}")

    if args.validate_only:
        print(f"review_gate: {EVIDENCE} for {slug!r} is valid and fresh.")
        return 0

    try:
        m = manifest.load_for(root, cfg, slug)
    except manifest.ManifestError as exc:
        return fail(f"manifest unreadable: {exc}")
    files = sorted(f for f in unit.rglob("*")
                   if f.is_file() and f.name != "manifest.yaml")
    hashes = {f.relative_to(unit).as_posix(): manifest.sha256_file(f)
              for f in files}
    try:
        m.set_gate("reviewed", sha256=hashes)
    except manifest.ManifestError as exc:
        return fail(str(exc))
    m.save(paths.manifest_path(root, cfg, slug))

    active = paths.read_active(root, cfg)
    if active and active.get("slug") == slug:
        paths.touch_marker(root, active.get("session_id"))  # arm the Stop gate

    nxt = m.next_gate()
    step = f" Next: {cfg.gate_command(nxt, slug)}" if nxt else " Unit complete."
    print(f"review_gate: reviewed set for {slug}.{step}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
