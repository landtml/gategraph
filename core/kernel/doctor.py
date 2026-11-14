"""Toolchain preflight: REQUIRED and ADVISORY results.

Built-in checks: POSIX platform, Python >= brand.MIN_PYTHON, PyYAML, a loadable
adapter, and the dependency lock: every installed version must equal the pin in
core/requirements.lock (a required failure when one differs or is missing).
Tracing variables (LangSmith) are a required failure under privacy "strict" and
an advisory note otherwise.

Each run caches its results under the state directory. A cache older than a
day is ignored and reported as stale (bug B52).

Usage: python3 -m core.kernel.doctor [--json] [--quiet] [--privacy strict] [--root DIR]
Exit codes: 0 all REQUIRED passed / 1 otherwise.
Standard library and PyYAML only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import Mapping

import yaml

from core import brand
from core.kernel import config, paths

CACHE_NAME = "doctor.json"
CACHE_MAX_AGE_S = 86400
TRACING_VARS = ("LANGSMITH_TRACING", "LANGSMITH_TRACING_V2",
                "LANGCHAIN_TRACING", "LANGCHAIN_TRACING_V2")
DEFAULT_LOCK = Path(__file__).resolve().parents[1] / "requirements.lock"
PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\;]+)")


@dataclass(frozen=True)
class Result:
    name: str
    ok: bool
    detail: str
    required: bool

    def as_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail,
                "required": self.required}


def _platform_check() -> Result:
    ok = not sys.platform.startswith(("win32", "cygwin", "msys"))
    detail = (f"{sys.platform} (POSIX)" if ok else
              f"{sys.platform} is unsupported, the harness is POSIX-only "
              f"(Linux/macOS); on Windows use WSL")
    return Result("platform", ok, detail, True)


def _python_check() -> Result:
    floor = f"{brand.MIN_PYTHON[0]}.{brand.MIN_PYTHON[1]}"
    have = f"{sys.version_info.major}.{sys.version_info.minor}"
    return Result("python", sys.version_info >= brand.MIN_PYTHON,
                  f"Python {have} (need >= {floor})", True)


def _yaml_check() -> Result:
    return Result("PyYAML", True, f"PyYAML {yaml.__version__}", True)


def _config_check(root: Path) -> Result:
    try:
        config.load(root)
    except config.ConfigError as exc:
        return Result("adapter", False, "; ".join(exc.problems), True)
    return Result("adapter", True, "loads and validates", True)


def read_pins(lock_path: Path) -> list[tuple[str, str]]:
    pins = []
    for line in lock_path.read_text(encoding="utf-8").splitlines():
        m = PIN_RE.match(line)
        if m:
            pins.append((m.group(1), m.group(2)))
    return pins


def _lock_check(lock_path: Path) -> Result:
    if not lock_path.is_file():
        return Result("dependency-lock", True,
                      f"no lock file at {lock_path}: not checked", False)
    mismatched = []
    pins = read_pins(lock_path)
    for name, pinned in pins:
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            installed = None
        if installed != pinned:
            mismatched.append(f"{name} installed {installed or 'nothing'}, pinned {pinned}")
    if mismatched:
        return Result("dependency-lock", False,
                      "differs from core/requirements.lock: " + "; ".join(mismatched)
                      + " (run `make venv`)", True)
    return Result("dependency-lock", True,
                  f"{len(pins)} pinned package(s) match the lock", True)


def _tracing_check(privacy: str | None, env: Mapping[str, str]) -> Result:
    strict = privacy == "strict"
    set_vars = [v for v in TRACING_VARS if env.get(v)]
    if not set_vars:
        return Result("tracing", True, "no tracing variable set", strict)
    detail = (f"{', '.join(set_vars)} set"
              + (" under privacy strict: unset it" if strict else ""))
    return Result("tracing", False, detail, strict)


def run_doctor(root, *, privacy: str | None = None, env: Mapping[str, str] | None = None,
               lock_path: Path | None = None) -> list[Result]:
    """Run every built-in check. privacy is "strict" or None; env defaults to
    the process environment; lock_path defaults to core/requirements.lock."""
    root = Path(root)
    env = os.environ if env is None else env
    return [
        _platform_check(),
        _python_check(),
        _yaml_check(),
        _config_check(root),
        _lock_check(Path(lock_path) if lock_path else DEFAULT_LOCK),
        _tracing_check(privacy, env),
    ]


def cache_path(root) -> Path:
    return paths.state_dir(root) / CACHE_NAME


def write_cache(root, results: list[Result], ok: bool, now: datetime | None = None) -> None:
    payload = {"at": (now or datetime.now()).replace(microsecond=0).isoformat(),
               "ok": ok, "checks": [r.as_dict() for r in results]}
    try:
        paths.atomic_write(cache_path(root), json.dumps(payload, indent=2) + "\n")
    except OSError:
        pass  # the cache is best-effort; never fail the doctor over it


def read_cache(root, now: datetime | None = None) -> tuple[dict | None, str]:
    """(payload, note). A cache older than CACHE_MAX_AGE_S is ignored: the
    payload is None and the note says it is stale (B52)."""
    try:
        payload = json.loads(cache_path(root).read_text(encoding="utf-8"))
        at = datetime.fromisoformat(payload["at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None, "no usable doctor cache"
    age = ((now or datetime.now()) - at).total_seconds()
    if age > CACHE_MAX_AGE_S:
        return None, (f"doctor cache is stale ({age / 86400:.1f} days old), "
                      f"ignored: run the doctor")
    return payload, "doctor cache is fresh"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Toolchain preflight.")
    p.add_argument("--root", default=None)
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--privacy", choices=["strict"], default=None)
    args = p.parse_args(argv)
    root = Path(args.root).resolve() if args.root else paths.find_root()

    results = run_doctor(root, privacy=args.privacy)
    required_fail = [r for r in results if r.required and not r.ok]
    advisory_fail = [r for r in results if not r.required and not r.ok]
    exit_code = 1 if required_fail else 0
    write_cache(root, results, exit_code == 0)

    if args.json:
        print(json.dumps({"ok": exit_code == 0, "root": str(root),
                          "checks": [r.as_dict() for r in results]}, indent=2))
        return exit_code
    if not (args.quiet and not required_fail and not advisory_fail):
        print(f"doctor: preflight for {root}\n")
        for r in results:
            mark = "ok" if r.ok else ("FAIL" if r.required else "warn")
            tag = "" if r.ok else ("  [REQUIRED]" if r.required else "  [advisory]")
            print(f"  {mark:<4} {r.name:<16} {r.detail}{tag}")
        print()
        if required_fail:
            print(f"doctor: FAILED: {len(required_fail)} required check(s) failed.",
                  file=sys.stderr)
        elif advisory_fail:
            print(f"doctor: OK with {len(advisory_fail)} advisory warning(s).")
        else:
            print("doctor: OK, toolchain ready.")
    return exit_code


if __name__ == "__main__":  # run from the repository root
    sys.exit(main())
