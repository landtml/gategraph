"""Style lint for the code pack (standard library only): trailing whitespace, long lines,
and a missing final newline, in every .py file of the unit.

Usage: python3 packs/code/checks/lint.py <unit-dir>

Exit 0 clean; exit 1 fatal, with one JSON finding per problem on stdout. This is a
small style gate, not a full linter; a project that needs pyflakes or ruff adds
its own check in its adapter.
"""
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True

MAX_LINE = 100


def emit(rule, message, path=None, line=None):
    print(json.dumps({"rule": rule, "severity": "fatal", "path": path, "line": line,
                      "message": message}))


def main(argv):
    if len(argv) != 2:
        print("usage: lint.py <unit-dir>", file=sys.stderr)
        return 1
    unit = Path(argv[1])
    if not unit.is_dir():
        emit("lint.no_unit", f"unit directory not found: {unit}")
        return 1
    failures = 0
    for path in sorted(unit.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            emit("lint.unreadable", f"cannot read: {exc}", path=str(path))
            failures += 1
            continue
        if text and not text.endswith("\n"):
            emit("lint.missing-final-newline", "file does not end with a newline", path=str(path))
            failures += 1
        for number, line in enumerate(text.splitlines(), start=1):
            if line != line.rstrip():
                emit("lint.trailing-whitespace", "trailing whitespace", path=str(path), line=number)
                failures += 1
            if len(line) > MAX_LINE:
                emit("lint.line-too-long", f"line is {len(line)} characters, limit {MAX_LINE}",
                     path=str(path), line=number)
                failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
