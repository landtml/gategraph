"""Host-neutral helpers for host translators (plan 1.16; brief H1).

A translator maps its host's payload to a HostEvent and a HostReply to its host's
output. These two helpers are what every translator shares: the tool-call mapping
and the flat decision fields, so a new host repeats neither.

Standard library and bridge only (kernel layer).
"""
from __future__ import annotations

from core.hosts import bridge
from core.hosts.bridge import HostReply


def tool_action(tool, tool_input: dict):
    """The kernel Action for a tool call, or None for a tool the kernel does not map."""
    return bridge._tool_action(tool, tool_input)


def decision_fields(reply: HostReply) -> dict:
    """Host-neutral reply fields: the verdict, the reason, the advisory context, the block flag."""
    return {"decision": reply.verdict.value, "reason": reply.reason,
            "context": reply.context, "block": reply.block}
