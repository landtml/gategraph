"""Variant-imports smoke for the code pack (a port of core/loop/smoke.py).

Usage: python3 packs/code/checks/imports.py <unit-dir>

The unit's variant.py must exist, be non-empty, and import cleanly. Exit 0
pass; exit 1 fatal, with one JSON finding per line on stdout. Standard library
only. Writes no bytecode, so the unit's surface is unchanged by this check.
"""
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True

VARIANT = "variant.py"


def emit(rule, message, path=None, line=None):
    print(json.dumps({"rule": rule, "severity": "fatal", "path": path, "line": line,
                      "message": message}))


def main(argv):
    if len(argv) != 2:
        print("usage: imports.py <unit-dir>", file=sys.stderr)
        return 1
    unit = Path(argv[1])
    if not unit.is_dir():
        emit("imports.no_unit", f"unit directory not found: {unit}")
        return 1
    variant = unit / VARIANT
    if not variant.is_file():
        emit("imports.no_variant", f"{VARIANT} is missing", path=str(variant))
        return 1
    if variant.stat().st_size == 0:
        emit("imports.empty", f"{VARIANT} is empty", path=str(variant))
        return 1
    try:
        source = variant.read_text(encoding="utf-8")
        code = compile(source, str(variant), "exec")
    except SyntaxError as exc:
        emit("imports.import_failed", f"SyntaxError: {exc.msg}", path=str(variant), line=exc.lineno)
        return 1
    except (OSError, UnicodeDecodeError) as exc:
        emit("imports.import_failed", f"unreadable: {exc}", path=str(variant))
        return 1
    # Sibling modules of the variant are importable from the unit directory.
    sys.path.insert(0, str(unit.resolve()))
    try:
        exec(code, {"__name__": "variant", "__file__": str(variant)})
    except Exception as exc:  # noqa: BLE001 - any import-time failure is a variant bug
        emit("imports.import_failed", f"{type(exc).__name__}: {exc}", path=str(variant))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
