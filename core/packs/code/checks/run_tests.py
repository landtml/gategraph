"""Unit tests for the code pack: unittest discovery over the unit (standard library only).

Usage: python3 packs/code/checks/run_tests.py <unit-dir>

Exit 0 pass; exit 1 fatal, with one JSON finding per line on stdout: tests.none
when no test_*.py file exists, tests.failed when unittest reports a failure or
error, tests.timeout when the run exceeds the limit. The tests run with
PYTHONDONTWRITEBYTECODE set, so the unit's surface is unchanged.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True

TIMEOUT_S = 60


def emit(rule, message, path=None):
    print(json.dumps({"rule": rule, "severity": "fatal", "path": path, "line": None,
                      "message": message}))


def main(argv):
    if len(argv) != 2:
        print("usage: run_tests.py <unit-dir>", file=sys.stderr)
        return 1
    unit = Path(argv[1])
    if not unit.is_dir():
        emit("tests.no_unit", f"unit directory not found: {unit}")
        return 1
    tests = [p for p in unit.rglob("test_*.py") if "__pycache__" not in p.parts]
    if not tests:
        emit("tests.none", f"no test_*.py file under {unit}")
        return 1
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    command = [sys.executable, "-B", "-m", "unittest", "discover",
               "-s", str(unit), "-t", str(unit), "-p", "test_*.py"]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, env=env, timeout=TIMEOUT_S)
    except subprocess.TimeoutExpired:
        emit("tests.timeout", f"unit tests timed out after {TIMEOUT_S}s")
        return 1
    if proc.returncode == 0:
        return 0
    lines = [line.strip() for line in proc.stderr.splitlines() if line.strip()]
    named = [line for line in lines if line.startswith(("FAIL:", "ERROR:"))][:3]
    detail = "; ".join(named) or (lines[-1] if lines else f"exit {proc.returncode}")
    emit("tests.failed", f"unit tests failed (unittest exit {proc.returncode}): {detail}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
