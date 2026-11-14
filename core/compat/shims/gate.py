"""Version-1 gate submission: who may attest a gate, and how the attestation reaches the runner.

``Manifest.set_gate`` in the shim writes no file (plan 1.18, "How legacy producers work after
migration"). It asks the runner to attest the gate through the gate command, which runs the
gate's checks and seals if they pass. Two rules decide whether that happens:

* The caller must be the gate's registered legacy producer: the script that the gate's check
  runs (the command recorded for the gate at migration). Any other caller is refused (bug B01).
* While the runner runs that check, the producer calls ``set_gate`` again, from inside the check.
  The runner marks that run in the environment (``<prefix>ATTESTING``), and the nested call
  returns at once, so the check's exit code decides the seal. An environment variable cannot
  bypass the producer rule, because the caller must still be the producer.

Kernel layer: standard library only. The runner is reached through its command, never imported.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

from core import brand

ATTEST_ENV = brand.ENV_PREFIX + "ATTESTING"
ATTEST_TIMEOUT_S = 900
CLI_RELATIVE = ("cli", "gate.py")  # core/cli/gate.py, next to compat/ in any copy of the core


def legacy_producer(kcfg, gate: str) -> str | None:
    """The command recorded for ``gate`` at migration (its check's command), or None."""
    spec = next((g for g in kcfg.gates if g.id == gate), None)
    if spec is None or not spec.checks:
        return None
    by_id = {c.id: c for c in kcfg.checks}
    check = by_id.get(spec.checks[0])
    return check.command if check is not None else None


def producer_script(root, command: str | None) -> str | None:
    """The real path of the script a producer command runs (its first ``*.py`` word), or None."""
    if not command:
        return None
    for word in shlex.split(command):
        if word.endswith(".py"):
            return os.path.realpath(os.path.join(os.fspath(root), word))
    return None


def is_caller(root, command: str | None) -> bool:
    """True when this process is the script the producer command runs."""
    script = producer_script(root, command)
    argv0 = sys.argv[0] if sys.argv else ""
    if script is None or not argv0 or argv0 == "-c":
        return False
    return os.path.realpath(argv0) == script


def guard_key(root, slug: str, gate: str) -> str:
    return f"{os.path.realpath(os.fspath(root))}|{slug}|{gate}"


def inside_attestation(root, slug: str, gate: str) -> bool:
    """True when the runner is already running this gate's check for this unit."""
    return os.environ.get(ATTEST_ENV) == guard_key(root, slug, gate)


def submit(root, slug: str, gate: str) -> tuple[int, str]:
    """Ask the runner to attempt ``gate`` for ``slug``. Returns (exit code, last message line)."""
    root = Path(root)
    cli = Path(__file__).resolve().parents[2].joinpath(*CLI_RELATIVE)
    venv_python = root / ".venv" / "bin" / "python"
    python = str(venv_python) if venv_python.exists() else sys.executable
    env = dict(os.environ)
    env[ATTEST_ENV] = guard_key(root, slug, gate)
    argv = [python, str(cli), "--root", str(root), "attempt", gate,
            "--unit", slug, "--by", "legacy-producer"]
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=ATTEST_TIMEOUT_S, env=env, cwd=str(root))
    except subprocess.TimeoutExpired:
        return 124, f"the runner did not finish the {gate} attempt in {ATTEST_TIMEOUT_S}s"
    text = (proc.stderr or proc.stdout or "").strip().splitlines()
    return proc.returncode, (text[-1] if text else "")
