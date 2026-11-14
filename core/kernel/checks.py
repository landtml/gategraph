"""Check runner: one check as a subprocess under the check exit-code contract (K5).

Exit codes: 0 pass; 1 fatal (warn when the entry declares ``severity: warn``);
2 warning; anything else, including 3, 126, 127 and death by signal, means the
check did not run, which is fatal. A timeout is fatal, except in hook mode when
the timeout was cut short by the shared budget: then the check is skipped with
advice, never failed.

Standard library only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import time
from collections.abc import Iterable, Sequence
from pathlib import Path

from core.kernel.globs import glob_match
from core.kernel.types import CheckResult, CheckSpec, CheckStatus, Finding, Severity

DEFAULT_TIMEOUT_S = 120.0
# Hook mode: a check that cannot start with at least this much budget left is skipped.
MIN_BUDGET_S = 1.0
# After a kill, how long to wait for the pipes to close before giving up on them.
DRAIN_S = 5.0
STDERR_FALLBACK_LINES = 3
SKIP_ADVICE = "run `make check` for full enforcement"
_PLACEHOLDER = re.compile(r"\{(files|unit|pack)\}")


def run_check(spec: CheckSpec, root: Path | str, *, files: Sequence[str] = (),
              unit: str = "", pack: str = "",
              deadline: float | None = None) -> CheckResult:
    """Run one check with ``root`` as its working directory and return its result.

    ``files`` (repo-relative), ``unit`` and ``pack`` replace ``{files}``,
    ``{unit}`` and ``{pack}`` in the command, each shell-quoted. ``deadline`` is
    an absolute ``time.monotonic()`` value shared by every check of one hook
    run; ``None`` (gate-earning runs) means no budget.
    """
    started = time.monotonic()
    root = Path(root)
    digest = _producer_digest(spec.command, root)
    timeout_s = DEFAULT_TIMEOUT_S if spec.timeout_s is None else float(spec.timeout_s)
    budget_limited = False
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining < MIN_BUDGET_S:
            return _result(spec, None, CheckStatus.SKIPPED,
                           (_skip_finding(spec.id),), started, digest)
        if remaining < timeout_s:
            timeout_s = remaining
            budget_limited = True

    command = _substitute(spec.command, files, unit, pack)
    try:
        proc = subprocess.Popen(command, shell=True, cwd=root, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, errors="replace",
                                start_new_session=True)
    except OSError as exc:
        finding = Finding(rule="check.did_not_run", severity=Severity.FATAL,
                          path=None, line=None,
                          message=f"[{spec.id}] could not start: {exc}")
        return _result(spec, None, CheckStatus.DID_NOT_RUN, (finding,), started, digest)

    try:
        out, err = proc.communicate(timeout=timeout_s)
        timed_out = False
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_group(proc)
        out, err = _drain(proc)
    finally:
        # Also reaps children the shell left behind after a normal exit.
        _kill_group(proc)

    if timed_out:
        if budget_limited:
            return _result(spec, None, CheckStatus.SKIPPED,
                           (_skip_finding(spec.id),), started, digest)
        marker = Finding(rule="check.timeout", severity=Severity.FATAL, path=None,
                         line=None,
                         message=f"[{spec.id}] check timed out after {timeout_s:.3g}s")
        findings = (marker,) + _findings(spec, CheckStatus.FATAL, out, err)
        return _result(spec, None, CheckStatus.FATAL, findings, started, digest)

    code = proc.returncode
    if code == 0:
        status = CheckStatus.PASS
    elif code == 1:
        status = CheckStatus.WARN if spec.severity == Severity.WARN else CheckStatus.FATAL
    elif code == 2:
        status = CheckStatus.WARN
    else:
        status = CheckStatus.DID_NOT_RUN
    findings = _findings(spec, status, out, err)
    if status == CheckStatus.DID_NOT_RUN:
        marker = Finding(rule="check.did_not_run", severity=Severity.FATAL, path=None,
                         line=None,
                         message=f"[{spec.id}] check did not run (exit {code})")
        findings = (marker,) + findings
    return _result(spec, code, status, findings, started, digest)


def select_checks(checks: Iterable[CheckSpec],
                  written: Iterable[str] | None = None) -> tuple[CheckSpec, ...]:
    """Pick the checks to run.

    ``written=None`` is a gate run: every check is selected. Otherwise (a write
    in hook mode) only checks whose non-empty ``on`` globs match a written path.
    Checks with no ``on`` are gate-only and are never write-triggered.
    """
    specs = tuple(checks)
    if written is None:
        return specs
    paths = [_normalize(p) for p in written]
    return tuple(spec for spec in specs
                 if spec.on and any(glob_match(g, p) for g in spec.on for p in paths))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _result(spec: CheckSpec, code: int | None, status: CheckStatus,
            findings: tuple, started: float, digest: str) -> CheckResult:
    return CheckResult(id=spec.id, exit=code, status=status, findings=tuple(findings),
                       duration_s=round(time.monotonic() - started, 3), digest=digest)


def _skip_finding(check_id: str) -> Finding:
    return Finding(rule="check.skipped", severity=Severity.WARN, path=None, line=None,
                   message=f"[{check_id}] SKIPPED in hook context "
                           f"(time budget exhausted), {SKIP_ADVICE}")


def _substitute(command: str, files: Sequence[str], unit: str, pack: str) -> str:
    """One pass over the template, so substituted values are never re-expanded."""
    values = {
        "files": " ".join(shlex.quote(f) for f in files),
        "unit": shlex.quote(unit),
        "pack": shlex.quote(pack),
    }
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], command)


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the whole process group (the shell and everything it forked)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _drain(proc: subprocess.Popen) -> tuple[str, str]:
    """Collect output after a group kill. Pipes held open by an escaped child are closed."""
    try:
        return proc.communicate(timeout=DRAIN_S)
    except subprocess.TimeoutExpired:
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
        proc.wait()
        return "", ""


def _findings(spec: CheckSpec, status: CheckStatus, out: str, err: str) -> tuple:
    """Findings from JSON on stdout; else (non-pass only) the first lines of stderr."""
    lines = [line for line in out.splitlines() if line.strip()]
    parsed = _json_findings(lines, spec.id)
    if parsed is not None:
        return tuple(parsed)
    if status == CheckStatus.PASS:
        return ()
    default = Severity.WARN if status == CheckStatus.WARN else Severity.FATAL
    text = [line for line in err.splitlines() if line.strip()]
    if not text:
        text = lines  # the lint contract: stderr, else stdout
    return tuple(Finding(rule="check.output", severity=default, path=None, line=None,
                         message=line)
                 for line in text[:STDERR_FALLBACK_LINES])


def _json_findings(lines: list, check_id: str) -> list | None:
    """One JSON object per line, all of them, or None when any line is not one."""
    if not lines:
        return None
    findings = []
    for line in lines:
        try:
            obj = json.loads(line)
        except ValueError:
            return None
        if not isinstance(obj, dict):
            return None
        findings.append(_finding_from_json(obj, check_id))
    return findings


def _finding_from_json(obj: dict, check_id: str) -> Finding:
    try:
        severity = Severity(obj.get("severity"))
    except ValueError:
        severity = Severity.FATAL
    rule = obj.get("rule")
    path = obj.get("path")
    line = obj.get("line")
    remedy = obj.get("remedy")
    return Finding(
        rule=rule if isinstance(rule, str) else check_id,
        severity=severity,
        path=path if isinstance(path, str) else None,
        line=line if isinstance(line, int) and not isinstance(line, bool) else None,
        message=str(obj.get("message", "")),
        remedy=remedy if isinstance(remedy, str) else None,
    )


def _producer_digest(command: str, root: Path) -> str:
    """sha256 of the command string, then the bytes of each in-root script it names."""
    digest = hashlib.sha256()
    digest.update(command.encode("utf-8"))
    for rel in _named_scripts(command, root):
        digest.update(b"\0" + rel.encode("utf-8") + b"\0")
        digest.update((root / rel).read_bytes())
    return digest.hexdigest()


def _named_scripts(command: str, root: Path) -> list[str]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return []
    base = root.resolve()
    found = set()
    for token in tokens:
        if token.startswith("-"):
            continue
        variant = (base / token).resolve() if not os.path.isabs(token) \
            else Path(token).resolve()
        try:
            rel = variant.relative_to(base).as_posix()
        except ValueError:
            continue  # interpreters and tools outside the repository are not hashed
        if variant.is_file():
            found.add(rel)
    return sorted(found)


def _normalize(path: str) -> str:
    path = path.replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path
