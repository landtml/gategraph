"""Claude Code host for the shims (plan 1.16; brief H1).

``run(kind, stdin_text, root=None)`` is the whole hook: parse the payload, load the
configuration, decide, render. It returns ``(stdout, stderr, exit_code)``.
``main(kind)`` is the stdin/stdout wrapper the shims call.

Failure rules (as the legacy hooks, with the brief's pre-tool choice):
* an unreadable pre-tool payload, or an internal error in a pre-tool decision, is refused
  (fail closed; the reply is a deny the host can read);
* an unreadable post-tool, stop or session-start payload fails open: exit 0, a note on stderr;
* no project root (no adapter file above the working directory or in the environment)
  means there is nothing to enforce: exit 0.

The configuration is ``config.load(root)``. When it cannot be loaded, the guard decides
with a stand-in that has only the root, and refuses writes outside ``harness/adapter/``.

Standard library, PyYAML and the kernel only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from core import brand
from core.hosts import bridge, dispatch
from core.hosts.bridge import HostReply
from core.kernel import config, paths
from core.kernel.types import Verdict

_PREFIX = brand.SLUG
UNREADABLE_PRE = ("the hook payload could not be read, so the action is refused "
                  "(fail-closed). Retry the action; if this repeats, check the host.")


class ClaudeCode(bridge.ClaudeCodeHost):
    """The bridge's claude_code host, with the flat decision keys beside the nested ones."""

    def render(self, reply: HostReply, kind: str) -> tuple[str, int]:
        text, code = super().render(reply, kind)
        if kind != "pre_tool" or not text:
            return text, code
        data: dict[str, Any] = json.loads(text)
        data["permissionDecision"] = reply.verdict.value
        data["permissionDecisionReason"] = reply.reason
        return json.dumps(data), code


# The registry entry for this host. Replaces the bridge's plain claude_code entry.
bridge.HOSTS[ClaudeCode.name] = ClaudeCode()
HOST = bridge.HOSTS[ClaudeCode.name]


class _Unloaded:
    """Stands in for the configuration when it cannot be loaded. It carries the root and
    no policy fields, so the guard refuses everything except writes under harness/adapter/."""

    def __init__(self, root: Path):
        self.root = root


def _refuse(kind: str, reason: str) -> HostReply:
    return HostReply(Verdict.DENY, reason=reason)


def run(kind: str, stdin_text: str, root: Path | str | None = None) -> tuple[str, str, int]:
    try:
        project = Path(root) if root is not None else paths.find_root()
    except paths.RootNotFound as exc:
        return "", f"{_PREFIX}: {exc}; nothing to enforce", 0

    try:
        event = HOST.parse(stdin_text)
        if event.kind != kind:
            raise ValueError(f"expected a {kind} payload, got {event.kind}")
    except ValueError as exc:
        if kind == "pre_tool":
            return _emit(HOST.render(_refuse(kind, UNREADABLE_PRE), kind))
        return "", f"{_PREFIX}: unreadable hook payload, failing open: {exc}", 0

    try:
        cfg = config.load(project)
    except Exception as exc:  # noqa: BLE001 - the guard decides without policy, with the root
        cfg = _Unloaded(project)
        diagnostic = f"{_PREFIX}: configuration not loaded: {exc}"
    else:
        diagnostic = ""

    try:
        reply = dispatch.handle(event, project, cfg)
    except Exception as exc:  # noqa: BLE001 - see the failure rules above
        if kind == "pre_tool":
            return _emit(HOST.render(_refuse(kind, UNREADABLE_PRE), kind))
        return "", f"{_PREFIX}: hook failed open: {exc}", 0

    out, code = HOST.render(reply, kind)
    stdout, stderr, code = _emit((out, code))
    return stdout, (stderr or diagnostic), code


def _emit(rendered: tuple[str, int]) -> tuple[str, str, int]:
    text, code = rendered
    if code == 2:
        return "", text, 2
    return text, "", code


# --------------------------------------------------------------------------
# Hook registration (D5). The adapter-complete check that used to live in harness_lint.
# --------------------------------------------------------------------------

SETTINGS_RELPATH = Path(".claude") / "settings.json"
SHIM_DIR = "core/hosts/shims"
REQUIRED_SHIMS = ("pretool_guard.py", "posttool_lint.py", "stop_gate.py", "session_start.py")
BASH_RELINT_FLAG = "--bash-relint"


def check_registration(root) -> list[str]:
    """Problems with the hook registration in ``<root>/.claude/settings.json``, as messages.

    An empty list means every shim is registered and the Bash relint is wired. The callers
    (harness_lint, selflint) turn each message into their own finding. Standard library only.
    """
    settings = Path(root) / SETTINGS_RELPATH
    if not settings.is_file():
        return [".claude/settings.json missing (hooks unregistered)"]
    try:
        blob = json.dumps(json.loads(settings.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError) as exc:
        return [f".claude/settings.json unreadable: {exc}"]
    out = []
    for shim in REQUIRED_SHIMS:
        if f"{SHIM_DIR}/{shim}" not in blob:
            out.append(f"hook not registered in settings.json: {shim}")
    if BASH_RELINT_FLAG not in blob:
        out.append("posttool_lint --bash-relint (Bash matcher) not registered: "
                   "the sed/tee bypass is open")
    return out


def main(kind: str) -> int:
    try:
        stdin_text = sys.stdin.read()
    except (OSError, UnicodeDecodeError):
        stdin_text = ""
    stdout, stderr, code = run(kind, stdin_text)
    if stdout:
        sys.stdout.write(stdout if stdout.endswith("\n") else stdout + "\n")
    if stderr:
        sys.stderr.write(stderr if stderr.endswith("\n") else stderr + "\n")
    return code
