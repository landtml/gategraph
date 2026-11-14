"""run_command: the execution tool (plan section 1.10, brief A2).

The command is an argument list, never a shell string. ``argv[0]`` must be a key
of the pack's allow-list, and it runs as the absolute path the pack mapped that
key to. No PATH lookup happens, so a planted executable in the unit cannot
shadow a listed name.

Confinement and reporting:
  * ``cwd`` is relative to the unit directory and must resolve inside it.
  * The child runs in its own session. A timeout, or the end of the call,
    SIGKILLs the whole process group (the same pattern as core/kernel/checks.py).
  * ``touched`` is the exact set of files whose content changed under the
    writable paths in ``ToolContext.writable``, found by a before-and-after
    listing (bug B36). Paths are workspace-relative, like ``ToolResult.touched``.
  * stdout and stderr go to unlinked temporary files, so memory stays bounded.
    The content is cut to ``ExecPolicy.max_output_chars`` with a marker.
  * The child environment is the parent's, minus every variable whose name ends
    in ``_KEY``, ``_TOKEN`` or ``_SECRET`` (case-insensitively), unless the policy
    lists that name in ``env_allow``.

Policy refusals and malformed arguments raise ``CommandRefused`` (a ValueError).
A tool body never calls ``interrupt()`` (plan section 1.12).

Standard library plus core.kernel only.
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import shlex
import stat
import subprocess
import tempfile
import time
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from core.kernel.checks import _kill_group
from core.kernel.types import Action, ActionKind
from core.tools.spec import ToolContext, ToolResult, ToolSpec

TOOL_NAME = "run_command"
SECRET_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET")
_MARKER_TEMPLATE = "\n[output truncated at {limit} characters]"
_MIN_OUTPUT_CHARS = 200
_CHUNK = 1 << 16
_ARGS_SCHEMA = {
    "type": "object",
    "properties": {
        "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "cwd": {"type": "string"},
        "timeout_s": {"type": "number", "exclusiveMinimum": 0},
    },
    "required": ["argv"],
    "additionalProperties": False,
}


class CommandRefused(ValueError):
    """The call breaks the policy (unlisted executable, bad cwd, bad timeout) or is malformed."""


@dataclasses.dataclass(frozen=True)
class ExecPolicy:
    """Per-pack configuration for run_command.

    ``allowed`` maps a bare executable name, the only spelling a call may use, to
    the absolute path it runs. ``env_allow`` names secret-suffixed variables that
    the child may inherit.
    """
    allowed: Mapping[str, str]
    env_allow: frozenset[str] = frozenset()
    default_timeout_s: float = 60.0
    max_timeout_s: float = 600.0
    max_output_chars: int = 20_000
    needs_approval: bool = False

    def __post_init__(self) -> None:
        allowed = dict(self.allowed)
        if not allowed:
            raise ValueError("ExecPolicy.allowed must list at least one executable")
        for name, path in allowed.items():
            if not isinstance(name, str) or not name or "/" in name or "\\" in name \
                    or "\0" in name or name in (".", ".."):
                raise ValueError(f"allow-list key must be a bare name, got {name!r}")
            if not isinstance(path, str) or not os.path.isabs(path):
                raise ValueError(f"allow-list value for {name!r} must be an absolute path")
        object.__setattr__(self, "allowed", allowed)
        object.__setattr__(self, "env_allow", frozenset(self.env_allow))
        if not (0 < self.max_timeout_s):
            raise ValueError("max_timeout_s must be positive")
        if not (0 < self.default_timeout_s <= self.max_timeout_s):
            raise ValueError("default_timeout_s must be positive and within max_timeout_s")
        if self.max_output_chars < _MIN_OUTPUT_CHARS:
            raise ValueError(f"max_output_chars must be at least {_MIN_OUTPUT_CHARS}")


# ---------------------------------------------------------------------------
# argument handling
# ---------------------------------------------------------------------------

def _parse(args: Any, policy: ExecPolicy) -> tuple[list[str], str, float]:
    """Validate the arguments and the allow-list. No filesystem access."""
    if not isinstance(args, dict):
        raise CommandRefused("arguments must be an object")
    unknown = set(args) - {"argv", "cwd", "timeout_s"}
    if unknown:
        raise CommandRefused(f"unknown argument(s): {', '.join(sorted(unknown))}")
    argv = args.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise CommandRefused("argv must be a non-empty list of strings")
    if any("\0" in a for a in argv):
        raise CommandRefused("argv must not contain NUL characters")
    if argv[0] not in policy.allowed:
        raise CommandRefused(f"executable {argv[0]!r} is not on the allow-list")
    cwd = args.get("cwd", ".")
    if not isinstance(cwd, str) or "\0" in cwd:
        raise CommandRefused("cwd must be a string")
    timeout = args.get("timeout_s", policy.default_timeout_s)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
        raise CommandRefused("timeout_s must be a number")
    if not (0 < timeout <= policy.max_timeout_s):
        raise CommandRefused(f"timeout_s must be in (0, {policy.max_timeout_s:g}]")
    return list(argv), cwd, float(timeout)


def command_action(args: Any, policy: ExecPolicy) -> Action:
    """The Action the guard judges. It validates the call but touches nothing."""
    argv, _cwd, _timeout = _parse(args, policy)
    return Action(kind=ActionKind.EXEC, tool=TOOL_NAME, command=shlex.join(argv))


def _confine(unit_dir: Path, cwd: str) -> Path:
    base = Path(unit_dir).resolve()
    target = (base / cwd).resolve()
    if not target.is_relative_to(base):
        raise CommandRefused(f"cwd {cwd!r} resolves outside the unit directory")
    if not target.is_dir():
        raise CommandRefused(f"cwd {cwd!r} is not a directory inside the unit")
    return target


# ---------------------------------------------------------------------------
# touched: before-and-after listing of the writable paths
# ---------------------------------------------------------------------------

def _digest(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _entry(path: str) -> tuple:
    mode = os.lstat(path).st_mode
    if stat.S_ISLNK(mode):
        return ("link", os.readlink(path))
    if stat.S_ISREG(mode):
        return ("file", _digest(path))
    return ("special", stat.S_IFMT(mode))  # devices, fifos, sockets: never read


def _check_writable(entry: Any) -> str:
    """A writable path is a non-empty relative path with no parent escape."""
    if not isinstance(entry, str) or entry == "":
        raise ValueError(f"writable path must be a non-empty string, got {entry!r}")
    windows = PureWindowsPath(entry)
    if os.path.isabs(entry) or windows.drive or windows.root:
        raise ValueError(f"writable path must be relative, got {entry!r}")
    if ".." in PurePosixPath(entry).parts or ".." in windows.parts:
        raise ValueError(f"writable path must stay inside the workspace, got {entry!r}")
    return entry


def snapshot(root: Path | str, writable: Iterable[str]) -> dict[str, tuple]:
    """Map each regular file, symlink or special file under the writable paths to its state.

    Keys are workspace-relative posix paths. Symlinks are recorded, never followed.
    """
    base = Path(root)
    found: dict[str, tuple] = {}
    for entry in writable:
        rel = _check_writable(entry)
        top = base / rel
        if not os.path.lexists(top):
            continue
        if os.path.islink(top) or not top.is_dir():
            found[top.relative_to(base).as_posix()] = _entry(str(top))
            continue
        for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
            for name in list(dirnames) + list(filenames):
                full = os.path.join(dirpath, name)
                key = Path(full).relative_to(base).as_posix()
                try:
                    found[key] = _entry(full)
                except OSError:
                    found[key] = ("gone",)
    return found


def changed_paths(before: Mapping[str, tuple], after: Mapping[str, tuple]) -> tuple[str, ...]:
    """Paths added, removed or whose state changed between two snapshots, sorted."""
    keys = set(before) | set(after)
    return tuple(sorted(k for k in keys if before.get(k) != after.get(k)))


# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------

def _is_secret_name(name: str) -> bool:
    return name.upper().endswith(SECRET_SUFFIXES)


def _child_env(policy: ExecPolicy) -> dict[str, str]:
    return {
        name: value
        for name, value in os.environ.items()
        if not _is_secret_name(name) or name in policy.env_allow
    }


# ---------------------------------------------------------------------------
# the tool body
# ---------------------------------------------------------------------------

def _limit(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = _MARKER_TEMPLATE.format(limit=limit)
    return text[: limit - len(marker)] + marker


def _read_stream(handle, byte_cap: int) -> str:
    handle.seek(0)
    data = handle.read(byte_cap)
    return data.decode("utf-8", errors="replace")


def run_command(args: Any, ctx: ToolContext, policy: ExecPolicy) -> ToolResult:
    """Run one allow-listed executable inside the unit and report what it changed."""
    argv, cwd, timeout_s = _parse(args, policy)
    workdir = _confine(ctx.unit_dir, cwd)
    executable = policy.allowed[argv[0]]
    if not os.access(executable, os.X_OK):
        raise CommandRefused(f"executable for {argv[0]!r} is not runnable")

    before = snapshot(ctx.root, ctx.writable)
    # Each stream holds at most 4 bytes per character the limit allows, plus slack,
    # so a cut stream always yields more characters than the limit.
    byte_cap = policy.max_output_chars * 4 + 4

    out_file = tempfile.TemporaryFile()
    err_file = tempfile.TemporaryFile()
    started = time.monotonic()
    try:
        try:
            proc = subprocess.Popen(
                [executable, *argv[1:]], cwd=workdir, env=_child_env(policy),
                stdin=subprocess.DEVNULL, stdout=out_file, stderr=err_file,
                shell=False, start_new_session=True)
        except OSError as exc:
            after = snapshot(ctx.root, ctx.writable)
            return ToolResult(content=f"could not start {argv[0]!r}: {exc}",
                              touched=changed_paths(before, after))

        timed_out = False
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
            proc.wait()
        finally:
            _kill_group(proc)  # the whole group, including anything the command left behind

        stdout = _read_stream(out_file, byte_cap)
        stderr = _read_stream(err_file, byte_cap)
    finally:
        out_file.close()
        err_file.close()

    if timed_out:
        head = f"timed out after {timeout_s:g}s; the process group was killed"
    else:
        head = f"exit {proc.returncode}"
    text = head + "\n" + stdout
    if stderr:
        text += "\n--- stderr ---\n" + stderr
    after = snapshot(ctx.root, ctx.writable)
    return ToolResult(content=_limit(text, policy.max_output_chars),
                      touched=changed_paths(before, after))


def make_run_command_tool(policy: ExecPolicy) -> ToolSpec:
    """The ToolSpec for run_command, bound to one pack's policy."""
    return ToolSpec(
        name=TOOL_NAME,
        description=("Run an allow-listed executable with an argument list (no shell). "
                     "The working directory is inside the unit. Output is truncated."),
        args_schema=_ARGS_SCHEMA,
        effects=frozenset({"exec", "write"}),
        to_action=lambda args: command_action(args, policy),
        run=lambda args, ctx: run_command(args, ctx, policy),
        idempotent=False,
        needs_approval=policy.needs_approval,
    )
