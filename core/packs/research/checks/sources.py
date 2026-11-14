#!/usr/bin/env python3
"""sources.py: the research pack's source check (sources-resolve).

Given the unit directory, checks that:
  - the unit has a ``sources/`` directory holding at least one file
  - every source file is non-blank UTF-8 text (a source that cannot be read
    cannot back a claim)
  - every source that a claim cites (``| source: <path>`` in any ``*.md`` of
    the unit, outside ``sources/``) resolves to a non-blank file under
    ``sources/``

Exit codes (the check contract): 0 pass; 1 fatal, with one JSON finding per
line on stdout: {"rule", "severity", "path", "line", "message"}.

Standard library only.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

CITED = re.compile(r"^\s*(?:[-*]\s+)?claim:.*\|\s*source:\s*(\S+)\s*$", re.IGNORECASE)


def emit(rule: str, message: str, path: str | None = None, line: int | None = None) -> None:
    print(json.dumps({"rule": rule, "severity": "fatal", "path": path,
                      "line": line, "message": message}))


def main(argv=None) -> int:
    args = sys.argv[1:] if argv is None else list(argv)
    if len(args) != 1:
        emit("sources.usage", "usage: sources.py UNIT_DIR")
        return 1
    unit = Path(args[0])
    if not unit.is_dir():
        emit("sources.no_unit", f"unit directory '{unit}' does not exist")
        return 1
    sources = unit / "sources"
    files = sorted(p for p in sources.rglob("*") if p.is_file()) if sources.is_dir() else []
    if not files:
        emit("sources.none", "no source files: a report needs at least one source under sources/",
             path=f"{unit.as_posix()}/sources")
        return 1

    failed = False
    usable = set()
    for path in files:
        rel = path.relative_to(unit).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            emit("sources.not_text", "source is not UTF-8 text", path=rel)
            failed = True
            continue
        if not text.strip():
            emit("sources.empty", "source file is blank", path=rel)
            failed = True
            continue
        usable.add(path.resolve())

    root = sources.resolve()
    for memo in sorted(unit.rglob("*.md")):
        if sources.resolve() in memo.resolve().parents:
            continue
        rel = memo.relative_to(unit).as_posix()
        for lineno, line in enumerate(memo.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            match = CITED.match(line)
            if match is None:
                continue
            cited = (unit / match.group(1)).resolve()
            if cited not in usable or root not in cited.parents:
                emit("sources.unresolved", f"cited source '{match.group(1)}' does not resolve to "
                     f"a usable file under sources/", path=rel, line=lineno)
                failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
