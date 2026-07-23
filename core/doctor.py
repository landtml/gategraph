#!/usr/bin/env python3
"""doctor.py, toolchain preflight (REQUIRED vs ADVISORY, from adapter.yaml).

Built-in required checks: POSIX platform, Python >= 3.9, PyYAML, a loadable
adapter.yaml.
Adapter checks come from doctor.required / doctor.advisory ({id, cmd} each;
ok == the command exits 0).

Every run caches results to harness/state/doctor.json so session_start can
report toolchain problems without re-running commands.

Exit codes: 0 all REQUIRED passed (advisory failures allowed) / 1 otherwise.
Usage: doctor.py [--json] [--quiet] [--root DIR]
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import config, paths  # noqa: E402

MIN_PYTHON = (3, 9)
CMD_TIMEOUT = 20
CACHE_REL = Path("harness") / "state" / "doctor.json"


class Result:
    __slots__ = ("name", "ok", "detail", "required")

    def __init__(self, name: str, ok: bool, detail: str, required: bool):
        self.name, self.ok, self.detail, self.required = name, ok, detail, required

    def as_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail,
                "required": self.required}


def _run_cmd_check(entry: dict, required: bool, root: Path) -> Result:
    name, cmd = entry.get("id", "?"), entry.get("cmd", "")
    try:
        proc = subprocess.run(cmd, shell=True, cwd=root, capture_output=True,
                              text=True, timeout=CMD_TIMEOUT)
        ok = proc.returncode == 0
        out = (proc.stdout or proc.stderr).strip().splitlines()
        detail = out[0] if ok and out else (f"`{cmd}` exited {proc.returncode}"
                                            if not ok else cmd)
    except subprocess.TimeoutExpired:
        ok, detail = False, f"`{cmd}` timed out after {CMD_TIMEOUT}s"
    except OSError as exc:
        ok, detail = False, f"`{cmd}` failed to run: {exc}"
    return Result(name, ok, detail, required)


FLAGS_MD = Path(__file__).resolve().parent / "loop" / "FLAGS.md"


def _flags_cli_pin() -> str | None:
    """The Claude Code CLI version FLAGS.md's worker semantics were verified
    against, FLAGS.md is the single source of truth for the pin."""
    try:
        m = re.search(r"CLI (\d+\.\d+\.\d+)", FLAGS_MD.read_text(encoding="utf-8"))
    except OSError:
        return None
    return m.group(1) if m else None


def _claude_pin_check(root: Path) -> Result:
    """ADVISORY, loop-enabled projects only: `claude` on PATH and matching the
    FLAGS.md pin. Drift means the loop's empirically-verified worker seam may
    no longer hold (Part 1 item 7)."""
    name = "claude-cli"
    if shutil.which("claude") is None:
        return Result(name, False, "claude CLI not found on PATH "
                      "(the loop worker seam needs it)", False)
    try:
        proc = subprocess.run(["claude", "--version"], capture_output=True,
                              text=True, timeout=CMD_TIMEOUT)
        out = (proc.stdout or proc.stderr).strip()
        m = re.search(r"(\d+\.\d+\.\d+)", out)
        got = m.group(1) if m else None
    except (subprocess.TimeoutExpired, OSError) as exc:
        return Result(name, False, f"`claude --version` failed: {exc}", False)
    pin = _flags_cli_pin()
    if pin is None:
        return Result(name, False, "cannot read the version pin from "
                      "core/loop/FLAGS.md", False)
    if got != pin:
        return Result(name, False,
                      f"claude {got or '?'} != FLAGS.md pin {pin}: worker "
                      f"flags were verified on {pin}; re-run "
                      f"`verify-flags --live` and update FLAGS.md", False)
    return Result(name, True, f"claude {got} matches FLAGS.md pin", False)


def _platform_check() -> Result:
    """REQUIRED: the harness is POSIX-only (Part 1 item 3), os.replace
    atomicity, shell=True checks, fcntl locks and signal semantics are all
    POSIX assumptions. An explicit no beats a silent maybe."""
    ok = not sys.platform.startswith(("win32", "cygwin", "msys"))
    detail = (f"{sys.platform} (POSIX)" if ok else
              f"{sys.platform} is unsupported, the harness is POSIX-only "
              f"(Linux/macOS); on Windows use WSL")
    return Result("platform", ok, detail, True)


def run_checks(root: Path) -> list[Result]:
    results = [_platform_check(), Result(
        "python", sys.version_info >= MIN_PYTHON,
        f"Python {sys.version_info.major}.{sys.version_info.minor} "
        f"(need >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]})", True)]
    try:
        import yaml  # noqa: F401
        results.append(Result("PyYAML", True, f"PyYAML {yaml.__version__}", True))
    except Exception as exc:  # pragma: no cover - environment dependent
        results.append(Result("PyYAML", False,
                              f"import failed: {exc} (pip install pyyaml)", True))
    try:
        cfg = config.load(root)
        results.append(Result("adapter.yaml", True, "loads and validates", True))
    except config.ConfigError as exc:
        results.append(Result("adapter.yaml", False, str(exc), True))
        return results  # adapter checks unavailable without config
    for entry in cfg.doctor_required:
        results.append(_run_cmd_check(entry, True, root))
    for entry in cfg.doctor_advisory:
        results.append(_run_cmd_check(entry, False, root))
    if cfg.loop.get("enabled"):
        results.append(_claude_pin_check(root))
    return results


def write_cache(root: Path, results: list[Result], ok: bool) -> None:
    try:
        payload = {"at": datetime.now().replace(microsecond=0).isoformat(),
                   "ok": ok, "checks": [r.as_dict() for r in results]}
        paths.atomic_write(root / CACHE_REL, json.dumps(payload, indent=2) + "\n")
    except OSError:
        pass  # the cache is best-effort; never fail doctor over it


def read_cache(root: Path) -> dict | None:
    try:
        return json.loads((root / CACHE_REL).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--root", default=None)
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)
    root = Path(args.root).resolve() if args.root else paths.find_repo_root()

    results = run_checks(root)
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
            mark = "✓" if r.ok else ("✗" if r.required else "!")
            tag = "" if r.ok else ("  [REQUIRED]" if r.required else "  [advisory]")
            print(f"  {mark} {r.name:<14} {r.detail}{tag}")
        print()
        if required_fail:
            print(f"doctor: FAILED: {len(required_fail)} required check(s) failed.",
                  file=sys.stderr)
        elif advisory_fail:
            print(f"doctor: OK with {len(advisory_fail)} advisory warning(s).")
        else:
            print("doctor: OK, toolchain ready.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
