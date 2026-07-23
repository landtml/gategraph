"""policy.py, compile adapter deny/ask policies and scan text against them.

All patterns live ONLY in adapter.yaml (config.py loads them); this module
compiles them, maps a repo-relative path to the policies in scope, and scans
text. A finding is (policy_id, severity, line_no, summary).

Scanning is two-pass over NFKC-normalized, zero-width-stripped text: per line,
plus a whole-text pass so a match spanning a line break still fires (attributed
to its first line). See docs/THREAT-MODEL.md for what this does NOT defend
against.
"""
from __future__ import annotations

import re
import unicodedata
from fnmatch import fnmatch
from pathlib import Path

Finding = tuple  # (id, severity, line_no, summary)

# Zero-width characters an accidental paste can smuggle between token chars:
# ZWSP, ZWNJ, ZWJ, word joiner, BOM/ZWNBSP.
_ZERO_WIDTH = dict.fromkeys(
    map(ord, "\u200b\u200c\u200d\u2060\ufeff"))


def normalize_for_scan(text: str) -> str:
    """NFKC-fold homoglyphs (fullwidth/superscript digits etc.) and strip
    zero-width characters. Newlines survive both, so line numbers computed on
    the normalized text match the original."""
    return unicodedata.normalize("NFKC", text).translate(_ZERO_WIDTH)


def compile_policies(cfg) -> list[dict]:
    """Compile cfg.policies.deny into scan-ready dicts."""
    out = []
    for p in cfg.deny:
        out.append({
            "id": p["id"],
            "regex": re.compile(p["regex"]),
            "severity": p.get("severity", "fatal"),
            "scope": list(p.get("scope") or ["any"]),
            "summary": p.get("summary", p["id"]),
            "enforces": p.get("enforces"),
            "pretool": bool(p.get("pretool", True)),
        })
    return out


def scope_match(scopes: list, rel_path: str) -> bool:
    """'any' matches everything; otherwise scopes are repo-relative globs."""
    rel = Path(rel_path).as_posix()
    return any(s == "any" or fnmatch(rel, s) for s in scopes)


def scan_text(text: str, policies: list[dict], rel_path: str) -> list[Finding]:
    norm = normalize_for_scan(text)
    lines = norm.splitlines()
    findings: list[Finding] = []
    seen: set[tuple] = set()

    def add(pol: dict, line_no: int) -> None:
        key = (pol["id"], line_no)
        if key not in seen:
            seen.add(key)
            findings.append((pol["id"], pol["severity"], line_no, pol["summary"]))

    for pol in policies:
        if not scope_match(pol["scope"], rel_path):
            continue
        for n, line in enumerate(lines, 1):
            if pol["regex"].search(line):
                add(pol, n)
        # Whole-text pass (F14): a match spanning a line break, e.g. a regex
        # whose \s* really does tolerate newlines, must fire too. Reported at
        # the match's first line; strictly additive over the per-line pass.
        for m in pol["regex"].finditer(norm):
            add(pol, norm.count("\n", 0, m.start()) + 1)
    findings.sort(key=lambda f: (f[2], f[0]))
    return findings


def scan_file(path: Path, root: Path, policies: list[dict]) -> list[Finding]:
    path, root = Path(path), Path(root).resolve()
    try:
        rel = path.resolve().relative_to(root).as_posix()
    except ValueError:
        return []  # outside the repo, not ours to police
    text = path.read_text(encoding="utf-8", errors="replace")
    return scan_text(text, policies, rel)


def ask_reason(cfg, rel_path: str) -> str | None:
    """Reason string if rel_path is under an ask-policy tree, else None."""
    rel = Path(rel_path).as_posix()
    for entry in cfg.ask:
        if any(fnmatch(rel, pat) for pat in entry.get("paths") or []):
            return entry.get("reason", "this path requires confirmation")
    return None


def is_protected(cfg, rel_path: str) -> bool:
    """True if rel_path matches a protected_paths glob (harness-writable only)."""
    rel = Path(rel_path).as_posix()
    return any(fnmatch(rel, pat) for pat in cfg.protected_paths)
