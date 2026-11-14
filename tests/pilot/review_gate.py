#!/usr/bin/env python3
"""review_gate.py, the `review` check of consumer-A's `reviewed` gate (schema 2).

A check returns a verdict through its exit code and never writes evidence (plan section 1.17).
The gate is earned by `gate attempt reviewed`, which runs this check and the policy scan; the
graph records the gate only when every check of that gate passes.

    review_gate.py --unit <slug>

Exit 0 when unit/review.json is schema-valid, has verdict `pass`, and its artifact_sha256 matches
the CURRENT note.md. Exit 1 otherwise, with the reason and the command that produces a fresh
review. Standard library and core.kernel only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[2]  # the directory that holds `core/`
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))

from core.kernel import config, paths  # noqa: E402

ARTIFACT = "note.md"          # this project's reviewable artifact (one per unit)
EVIDENCE = "review.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
VERDICTS = {"pass", "fail", "tie"}
SEVERITIES = {"fatal", "warn", "note"}


def fail(msg: str) -> int:
    print(f"review_gate: {msg}", file=sys.stderr)
    return 1


def reviewer_cmd(unit_dir: str, slug: str) -> str:
    return ("python3 harness/adapter/checks/cross_review.py "
            f"--artifact {unit_dir}/{slug}/{ARTIFACT} "
            "--rubric harness/adapter/review_rubric.md "
            f"--out {unit_dir}/{slug}/{EVIDENCE}")


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


def sha256_bytes(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--unit", required=True)
    args = p.parse_args(argv)

    root = HARNESS.parent
    try:
        cfg = config.load(root)
    except config.ConfigError as exc:
        return fail(str(exc))

    slug = args.unit
    if not paths.valid_slug(slug):
        return fail(f"invalid unit slug {slug!r}")
    unit = paths.unit_path(root, cfg.unit_dir, slug)
    artifact = unit / ARTIFACT
    review_path = unit / EVIDENCE
    rerun = reviewer_cmd(cfg.unit_dir, slug)

    if not review_path.is_file():
        return fail(f"no {EVIDENCE} for {slug!r}; run the blinded reviewer first: {rerun}")
    try:
        review = json.loads(review_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return fail(f"{EVIDENCE} unreadable: {exc}")

    problem = validate(review)
    if problem:
        return fail(problem)
    if review["verdict"] != "pass":
        return fail(f"review verdict is {review['verdict']!r}; address the findings, "
                    f"then re-run: {rerun}")
    if not artifact.is_file():
        return fail(f"artifact missing: {cfg.unit_dir}/{slug}/{ARTIFACT}")
    if sha256_bytes(artifact) != review["artifact_sha256"]:
        return fail(f"artifact changed since review, {ARTIFACT} no longer matches "
                    f"artifact_sha256; re-run: {rerun}")
    print(f"review_gate: {EVIDENCE} for {slug!r} is valid and fresh.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
