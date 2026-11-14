"""Policy scanner: compile deny policies, scan text and artifacts against them.

Port of core/policy.py with the same verdicts (the detection corpus is the
regression net). Changes: scope patterns use the kernel glob dialect (B34);
files are read through the artifact readers, so binary files are not scanned
as text and PDFs are scanned as extracted text (B11); ``normalize`` is the one
normalization the pre-write guard and the scanner both use (B10).

Scanning is two-pass over normalized text: per line, plus a whole-text pass so
a match spanning a line break still fires (attributed to its first line).
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import NamedTuple, Sequence

from core.kernel import extract
from core.kernel.globs import glob_match, normalize_path
from core.kernel.types import Finding, Severity

# Zero-width characters an accidental paste can smuggle between token chars:
# ZWSP, ZWNJ, ZWJ, word joiner, BOM/ZWNBSP.
_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"))

_SEVERITIES = {s.value for s in Severity}


class PathScan(NamedTuple):
    findings: list[Finding]
    notes: list[dict]   # {"path", "note", "reason", "sha256"} for skipped artifacts


def normalize(text: str) -> str:
    """NFKC-fold homoglyphs (fullwidth digits, superscripts) and strip
    zero-width characters. Newlines survive, so line numbers match the original."""
    return unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH)


def compile_policies(cfg) -> list[dict]:
    """Compile cfg.deny into scan-ready dicts. An unknown severity is refused."""
    out = []
    for p in cfg.deny:
        severity = p.get("severity", "fatal")
        if severity not in _SEVERITIES:
            raise ValueError(f"deny policy {p.get('id')!r}: unknown severity "
                             f"{severity!r} (one of {sorted(_SEVERITIES)})")
        out.append({
            "id": p["id"],
            "regex": re.compile(p["regex"]),
            "severity": severity,
            "scope": list(p.get("scope") or ["any"]),
            "summary": p.get("summary", p["id"]),
            "enforces": p.get("enforces"),
            "pretool": bool(p.get("pretool", True)),
        })
    return out


def scope_match(scopes: Sequence[str], rel_path: str) -> bool:
    """'any' matches everything; otherwise scopes are repo-relative globs."""
    rel = normalize_path(rel_path)
    return any(s == "any" or glob_match(s, rel) for s in scopes)


def scan_text(text: str, policies: list[dict], rel_path: str) -> list[Finding]:
    norm = normalize(text)
    lines = norm.splitlines()
    rel = normalize_path(rel_path)
    findings: list[Finding] = []
    seen: set[tuple] = set()

    def add(pol: dict, line_no: int) -> None:
        key = (pol["id"], line_no)
        if key not in seen:
            seen.add(key)
            findings.append(Finding(rule=pol["id"], severity=Severity(pol["severity"]),
                                    path=rel, line=line_no, message=pol["summary"]))

    for pol in policies:
        if not scope_match(pol["scope"], rel):
            continue
        for n, line in enumerate(lines, 1):
            if pol["regex"].search(line):
                add(pol, n)
        # Whole-text pass: a match spanning a line break fires too, reported at
        # the match's first line. Strictly additive over the per-line pass.
        for m in pol["regex"].finditer(norm):
            add(pol, norm.count("\n", 0, m.start()) + 1)
    findings.sort(key=lambda f: (f.line, f.rule))
    return findings


def scan_path(path, root, policies: list[dict], pdf_command=None) -> PathScan:
    """Scan one file through its artifact reader. Files outside ``root`` are not
    ours to police and yield nothing. Skipped artifacts yield a note, not a finding."""
    path, root = Path(path), Path(root).resolve()
    try:
        rel = path.resolve().relative_to(root).as_posix()
    except ValueError:
        return PathScan([], [])
    art = extract.read_artifact(path, pdf_command=pdf_command)
    if art.text is None:
        return PathScan([], [{"path": rel, "note": art.note,
                              "reason": art.reason, "sha256": art.sha256}])
    return PathScan(scan_text(art.text, policies, rel), [])
