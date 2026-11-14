"""Harness tools for the native actor (plan section 1.10, Harness group, brief A1).

Tools: gate_status, request_gate, note_decision, commit_fact, escalate.

No tool here sets a gate. Gates are evaluated by the run-check node and recorded
by the seal node, never by a tool. None of these tools writes into the workspace,
so ``touched`` is always empty. Each returns a JSON record in ``content``; the
graph node that made the call stores that record.

``gate_status`` reads ``<scratch_dir>/gates.json`` if the runner has written it.
This tool only reads it.

Standard library, core.kernel and core.tools only. No import of the surface or
evidence modules, so this file cannot reach the gate store.
"""
from __future__ import annotations

import json
import pathlib

from core.kernel.types import Action, ActionKind
from core.tools.registry import ToolRefused
from core.tools.spec import ToolContext, ToolResult, ToolSpec

GATE_STATUS_FILE = "gates.json"


def _text(args: dict, key: str) -> str:
    value = args[key]
    if not isinstance(value, str) or value.strip() == "":
        raise ToolRefused(f"argument {key!r} must be a non-empty string")
    return value


def _record(payload: dict) -> ToolResult:
    return ToolResult(content=json.dumps(payload, sort_keys=True, ensure_ascii=False))


def gate_status(args: dict, ctx: ToolContext) -> ToolResult:
    path = pathlib.Path(ctx.scratch_dir) / GATE_STATUS_FILE
    if not path.is_file():
        return _record({"gates": None, "source": "absent"})
    try:
        gates = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return _record({"gates": None, "source": "unreadable", "error": str(exc)})
    return _record({"gates": gates, "source": GATE_STATUS_FILE})


def request_gate(args: dict, ctx: ToolContext) -> ToolResult:
    """Ends the work step. The gate itself is run and sealed by the graph."""
    return _record({"gate": _text(args, "gate"), "work_step": "finished",
                    "note": args.get("note", "")})


def note_decision(args: dict, ctx: ToolContext) -> ToolResult:
    return _record({"record": "decision", "text": _text(args, "text")})


def commit_fact(args: dict, ctx: ToolContext) -> ToolResult:
    return _record({"record": "fact", "key": _text(args, "key"), "value": args["value"]})


def escalate(args: dict, ctx: ToolContext) -> ToolResult:
    return _record({"record": "escalation", "reason": _text(args, "reason")})


def _spec(name, description, properties, required, run) -> ToolSpec:
    def to_action(args: dict) -> Action:
        return Action(kind=ActionKind.READ, tool=name)

    return ToolSpec(
        name=name,
        description=description,
        args_schema={"type": "object", "properties": properties,
                     "required": list(required), "additionalProperties": False},
        effects=frozenset({"read"}),
        to_action=to_action,
        run=run,
        idempotent=True,
    )


def specs() -> tuple[ToolSpec, ...]:
    text = {"type": "string"}
    return (
        _spec("gate_status", "Report the gate status the runner last wrote.",
              {}, [], gate_status),
        _spec("request_gate", "Finish this work step so the gate can be run.",
              {"gate": text, "note": text}, ["gate"], request_gate),
        _spec("note_decision", "Record a decision and its reason.",
              {"text": text}, ["text"], note_decision),
        _spec("commit_fact", "Record a fact the later steps may rely on.",
              {"key": text, "value": text}, ["key", "value"], commit_fact),
        _spec("escalate", "Ask a human for help with this unit.",
              {"reason": text}, ["reason"], escalate),
    )
