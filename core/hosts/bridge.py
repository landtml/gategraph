"""Host event contract: the types and the dispatch table of the host layer.

Flow (section 1.16):

    host event (JSON on stdin) -> Host.parse -> HostEvent
    HostEvent -> kernel decision (guard, post-write scan, session rules) -> HostReply
    HostReply -> Host.render(reply, kind) -> (stdout text, exit code)

Standard library only (kernel layer). Nothing here imports LangChain or LangGraph.

Rendering contract for the ``claude_code`` host (the shape the existing hooks print):

* ``pre_tool``: ALLOW prints nothing and exits 0. DENY or ASK prints one JSON object
  ``{"hookSpecificOutput": {"hookEventName": "PreToolUse",
  "permissionDecision": "deny"|"ask", "permissionDecisionReason": reason}}`` and exits 0.
* ``post_tool``: ``block`` writes the reason as text and exits 2 (the shim sends it to
  stderr, where the host shows it to the model). Otherwise ``context`` is printed as
  advisory text on exit 0.
* ``stop``: ``block`` prints ``{"decision": "block", "reason": reason}`` and exits 0.
  Otherwise nothing, exit 0.
* ``session_start``: ``context`` is printed as plain text on exit 0.

Hooks fail open: a host shim that cannot parse its input exits 0 and logs.
"""
from __future__ import annotations

import dataclasses
import json
from typing import Any, Protocol, runtime_checkable

from core.kernel.types import Action, ActionKind, Verdict

EVENT_KINDS = ("pre_tool", "post_tool", "stop", "session_start")


@dataclasses.dataclass(frozen=True)
class HostEvent:
    """One normalized host event. ``action`` is None for events without a tool call."""
    kind: str
    session: str | None
    tool: str | None
    action: Action | None
    raw: dict

    def __post_init__(self) -> None:
        if self.kind not in EVENT_KINDS:
            raise ValueError(f"HostEvent.kind must be one of {EVENT_KINDS}, got {self.kind!r}")
        if self.action is not None and not isinstance(self.action, Action):
            raise TypeError("HostEvent.action must be an Action or None")


@dataclasses.dataclass(frozen=True)
class HostReply:
    """What the kernel decided. ``render`` turns it into the host's expected output."""
    verdict: Verdict
    reason: str = ""
    context: str | None = None
    block: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "verdict", Verdict(self.verdict))


@runtime_checkable
class Host(Protocol):
    """One host program. Adding a host means one class and one registry entry."""

    name: str

    def parse(self, stdin_text: str) -> HostEvent:
        """Parse the host's stdin payload. Raises ValueError on a payload it cannot read."""
        ...

    def render(self, reply: HostReply, kind: str) -> tuple[str, int]:
        """Return ``(stdout_text, exit_code)`` for an event of ``kind``."""
        ...


# --------------------------------------------------------------------------
# claude_code
# --------------------------------------------------------------------------

_CLAUDE_EVENTS = {
    "PreToolUse": "pre_tool",
    "PostToolUse": "post_tool",
    "Stop": "stop",
    "SessionStart": "session_start",
}
_CLAUDE_NAMES = {kind: name for name, kind in _CLAUDE_EVENTS.items()}


def _text(mapping: dict, key: str) -> str | None:
    value = mapping.get(key)
    return value if isinstance(value, str) else None


def _tool_action(tool: str | None, tool_input: dict) -> Action | None:
    """Map a Claude Code tool call to a kernel Action. Unknown tools map to None."""
    if tool == "Write":
        return Action(kind=ActionKind.WRITE, tool=tool,
                      path=_text(tool_input, "file_path"),
                      content=_text(tool_input, "content"))
    if tool == "Edit":
        return Action(kind=ActionKind.EDIT, tool=tool,
                      path=_text(tool_input, "file_path"),
                      content=_text(tool_input, "new_string"))
    if tool == "NotebookEdit":
        return Action(kind=ActionKind.EDIT, tool=tool,
                      path=_text(tool_input, "notebook_path"),
                      content=_text(tool_input, "new_source"))
    if tool == "Bash":
        return Action(kind=ActionKind.EXEC, tool=tool,
                      command=_text(tool_input, "command"))
    if tool == "Read":
        return Action(kind=ActionKind.READ, tool=tool,
                      path=_text(tool_input, "file_path"))
    if tool == "WebFetch":
        return Action(kind=ActionKind.NET, tool=tool,
                      url=_text(tool_input, "url"))
    return None


class ClaudeCodeHost:
    """The Claude Code hook payload (fields: session_id, hook_event_name, tool_name,
    tool_input; see core/tests/hosts/events for recorded samples)."""

    name = "claude_code"

    def parse(self, stdin_text: str) -> HostEvent:
        data: Any = json.loads(stdin_text)
        if not isinstance(data, dict):
            raise ValueError("hook payload must be a JSON object")
        host_event = data.get("hook_event_name")
        kind = _CLAUDE_EVENTS.get(host_event)
        if kind is None:
            raise ValueError(f"unknown hook_event_name: {host_event!r}")
        session = _text(data, "session_id")
        tool = _text(data, "tool_name") if kind in ("pre_tool", "post_tool") else None
        action = None
        if kind in ("pre_tool", "post_tool"):
            tool_input = data.get("tool_input")
            action = _tool_action(tool, tool_input if isinstance(tool_input, dict) else {})
        return HostEvent(kind=kind, session=session, tool=tool,
                         action=action, raw=data)

    def render(self, reply: HostReply, kind: str) -> tuple[str, int]:
        if kind == "pre_tool":
            if reply.verdict is Verdict.ALLOW:
                return ("", 0)
            return (json.dumps({"hookSpecificOutput": {
                "hookEventName": _CLAUDE_NAMES["pre_tool"],
                "permissionDecision": reply.verdict.value,
                "permissionDecisionReason": reply.reason,
            }}), 0)
        if kind == "post_tool":
            if reply.block:
                return (reply.reason, 2)
            return (reply.context or "", 0)
        if kind == "stop":
            if reply.block:
                return (json.dumps({"decision": "block", "reason": reply.reason}), 0)
            return ("", 0)
        if kind == "session_start":
            return (reply.context or "", 0)
        raise ValueError(f"unknown event kind: {kind!r}")


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------

HOSTS: dict[str, Host] = {ClaudeCodeHost.name: ClaudeCodeHost()}
