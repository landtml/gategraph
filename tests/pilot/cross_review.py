#!/usr/bin/env python3
"""cross_review.py, the consumer-A blinded reviewer (plan §11, adapter-owned).

Reads ONLY the two paths it is given (the artifact and the rubric) and writes
the §11 review evidence file:

    {"artifact_sha256": <sha256 of the artifact bytes>,
     "verdict": "pass" | "fail",
     "findings": [{"criterion", "evidence", "severity: fatal|warn|note"}]}

This deterministic implementation tariffs checkable proxies of the rubric's
criteria (complete / internally consistent / evidence-backed); a cross-model
reviewer wired via reviewer.cross_model.cmd must produce the same shape.

Exit 0 whenever a review was produced: a fail verdict is still a successful
review run. Nonzero (2) only when the review could not run at all (bad paths).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

PLACEHOLDER_RE = re.compile(r"\b(TODO|FIXME|XXX|TBD)\b|placeholder|lorem ipsum",
                            re.IGNORECASE)
SOURCE_LINE_RE = re.compile(r"^\s*[-*]\s*source\s*:", re.IGNORECASE | re.MULTILINE)
MIN_BODY_LINES = 3


def review(artifact: Path, rubric_text: str) -> dict:
    raw = artifact.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    nonempty = [l for l in lines if l.strip()]
    findings = []

    # Complete: a titled note with an actual body.
    if not (nonempty and nonempty[0].startswith("# ")):
        findings.append({"criterion": "complete", "severity": "fatal",
                         "evidence": f"no `# ` title; first line is "
                                     f"{nonempty[0]!r}" if nonempty else
                                     "artifact is empty"})
    if len(nonempty) < MIN_BODY_LINES:
        findings.append({"criterion": "complete", "severity": "fatal",
                         "evidence": f"only {len(nonempty)} non-empty line(s) "
                                     f", not a written-out note"})

    # Internally consistent: no unfinished-work markers left in the artifact.
    for n, line in enumerate(lines, 1):
        if PLACEHOLDER_RE.search(line):
            findings.append({"criterion": "internally-consistent",
                             "severity": "fatal",
                             "evidence": f"line {n} is unfinished work: "
                                         f"{line.strip()!r}"})

    # Evidence-backed: at least one cited source line.
    if not SOURCE_LINE_RE.search(text):
        findings.append({"criterion": "evidence-backed", "severity": "fatal",
                         "evidence": "no `- source:` line, claims are uncited"})

    verdict = "fail" if any(f["severity"] == "fatal" for f in findings) else "pass"
    return {"artifact_sha256": hashlib.sha256(raw).hexdigest(),
            "verdict": verdict,
            "findings": findings}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--artifact", required=True)
    p.add_argument("--rubric", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args(argv)

    artifact, rubric, out = Path(args.artifact), Path(args.rubric), Path(args.out)
    if not artifact.is_file():
        print(f"cross_review: artifact not found: {artifact}", file=sys.stderr)
        return 2
    try:
        rubric_text = rubric.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"cross_review: cannot read rubric: {exc}", file=sys.stderr)
        return 2
    if not rubric_text.strip():
        print(f"cross_review: rubric is empty: {rubric}", file=sys.stderr)
        return 2

    result = review(artifact, rubric_text)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"cross_review: verdict={result['verdict']} "
          f"({len(result['findings'])} finding(s)) -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
