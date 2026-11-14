"""The work node (plan sections 1.5, 1.11, 1.12 and 1.16).

Two actors share this node, chosen by ``state["actor"]``:

- ``native``: the registered WorkActor is called by name. Its summary and spend
  reach UnitState, and so does one routing decision: when ``finished`` is True,
  ``target_gate`` is set (to ``gate``, or the first ready gate when ``gate`` is
  None). An unknown gate or an unfinished step becomes one fatal finding instead.
  Message content never reaches state.
- ``external``: one ``interrupt()`` of kind ``work`` names the gate to aim for
  and the current findings. The node proceeds on ``attempt`` and sets
  ``target_gate``. Any other answer leaves the unit paused at the request.

A variant fault from a native actor (core/kernel/faults.py) becomes a fatal
finding. An infrastructure fault propagates. Nothing else is caught.

Proceed rule (plan 1.5): the only side effect before ``interrupt()`` is reading
state. The native path has no interrupt, so its try/except is safe.

Config reads: ``cfg`` is a core.kernel.config.Config (gates as GateSpec) or a
mapping. Two optional keys have no field in Config yet: ``work_actor`` (the
registry name of the native actor, default ``DEFAULT_NATIVE_ACTOR``) and
``goals`` (gate id to goal text). Missing goals fall back to a generated line.

Imports: core.graph and core.kernel only, plus langgraph.types.interrupt. This
file never imports core.agent, so the native actor is found through the
registry, not by import.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any, Mapping

from langgraph.types import interrupt

from core.graph.contracts import WorkActor, WorkResult
from core.graph.state import UnitState
from core.kernel.faults import classify
from core.kernel.types import (
    Finding,
    FaultKind,
    FaultRoute,
    HumanAnswer,
    HumanKind,
    HumanRequest,
    Severity,
    Spend,
)

DEFAULT_NATIVE_ACTOR = "native"

# Spend fields that add up across attempts. first_started_at keeps the earliest value.
_SPEND_SUM = ("usd", "tokens", "model_calls", "tool_calls", "iterations", "elapsed_s")

_WORK_ACTORS: dict[str, WorkActor] = {}


def register_work_actor(name: str, actor: WorkActor) -> None:
    """Register a native actor under ``name``. Raises TypeError if it lacks ``run``."""
    if not isinstance(actor, WorkActor):
        raise TypeError(f"work actor {name!r} does not implement WorkActor.run")
    _WORK_ACTORS[name] = actor


def lookup_work_actor(name: str) -> WorkActor:
    """Return the actor registered under ``name``. Raises LookupError if there is none."""
    try:
        return _WORK_ACTORS[name]
    except KeyError:
        raise LookupError(f"no work actor registered as {name!r}") from None


def _opt(cfg: Any, key: str, default: Any) -> Any:
    if isinstance(cfg, Mapping):
        return cfg.get(key, default)
    return getattr(cfg, key, default)


def _gate_needs(cfg: Any) -> list[tuple[str, tuple[str, ...]]]:
    """(gate id, needs) pairs in declared order. A bare string gate has no needs."""
    out: list[tuple[str, tuple[str, ...]]] = []
    for gate in _opt(cfg, "gates", ()) or ():
        if isinstance(gate, str):
            out.append((gate, ()))
        else:
            out.append((gate.id, tuple(getattr(gate, "needs", ()) or ())))
    return out


def _first_ready(state: Mapping[str, Any], cfg: Any) -> str:
    """The first gate in declared order that is not effective and whose needs all are."""
    effective = set(state.get("effective") or ())
    for gate_id, needs in _gate_needs(cfg):
        if gate_id not in effective and all(n in effective for n in needs):
            return gate_id
    raise RuntimeError("work: no gate is ready; dispatch should have finalized the unit")


def _target(state: Mapping[str, Any], cfg: Any) -> str:
    """The gate to aim for: the one already named, else the first ready gate in order."""
    named = state.get("target_gate")
    if named:
        return named
    return _first_ready(state, cfg)


def _goal(cfg: Any, gate: str) -> str:
    goals = _opt(cfg, "goals", None) or {}
    return goals.get(gate) or f"Make gate {gate} pass."


def _fold_spend(prior: Mapping[str, Any] | None, add: Spend) -> dict:
    """Add one actor's spend to the unit's running total. Never raises on a missing key."""
    base = dict(prior or {})
    total = {key: (base.get(key) or 0) + getattr(add, key) for key in _SPEND_SUM}
    total["first_started_at"] = base.get("first_started_at") or add.first_started_at
    return Spend(**total).to_dict()


def _fault_finding(route: FaultRoute) -> dict:
    return Finding(
        rule="work.variant-fault",
        severity=Severity.FATAL,
        path=None,
        line=None,
        message=route.reason,
    ).to_dict()


def _unfinished_finding(summary: str) -> dict:
    return Finding(
        rule="work.unfinished",
        severity=Severity.FATAL,
        path=None,
        line=None,
        message=f"work actor did not finish: {summary}",
    ).to_dict()


def _unknown_gate_finding(gate: str) -> dict:
    return Finding(
        rule="work.unknown_gate",
        severity=Severity.FATAL,
        path=None,
        line=None,
        message=f"work actor asked for gate {gate!r}, which is not in the configuration",
    ).to_dict()


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _answer_of(proceed: Any) -> Any:
    if isinstance(proceed, Mapping):
        return proceed.get("answer")
    return proceed


def _external(state: Mapping[str, Any], target: str, findings: list[dict]) -> dict:
    request = HumanRequest(
        id=f"work-{target}-{(state.get('attempts') or {}).get(target, 0)}",
        kind=HumanKind.WORK,
        gate=target,
        channel="tty",
        summary=f"Make gate {target} pass, then answer attempt.",
        at=_now(),
        payload={"gate": target, "findings": findings},
    )
    # The only interrupt in this node. Nothing with a side effect runs before it.
    answer = interrupt(request.to_dict())
    if _answer_of(answer) != HumanAnswer.ATTEMPT.value:
        return {}
    return {"target_gate": target}


def _native(state: Mapping[str, Any], cfg: Any, target: str, findings: list[dict]) -> dict:
    actor = lookup_work_actor(_opt(cfg, "work_actor", DEFAULT_NATIVE_ACTOR))
    context = {"project": state.get("project"), "unit": state.get("unit"), "gate": target}
    try:
        result = actor.run(_goal(cfg, target), findings, context)
    except Exception as exc:
        route = classify(exc)
        if route.fault is FaultKind.VARIANT:
            return {"findings": [_fault_finding(route)]}
        raise
    if not isinstance(result, WorkResult):
        raise TypeError("work actor must return WorkResult")
    update: dict[str, Any] = {
        "work_summary": result.summary,
        "spend": _fold_spend(state.get("spend"), result.spend),
    }
    if not result.finished:
        # No target: route.next_step sends the unit back to work. The finding is the
        # feedback the actor gets on its next run.
        update["findings"] = [_unfinished_finding(result.summary)]
    elif result.gate is None:
        update["target_gate"] = _first_ready(state, cfg)
    elif result.gate in {gate_id for gate_id, _ in _gate_needs(cfg)}:
        update["target_gate"] = result.gate
    else:
        update["findings"] = [_unknown_gate_finding(result.gate)]
    return update


def work(state: UnitState, cfg: Mapping[str, Any]) -> dict:
    """The work node. Returns a state update; see the module docstring for each actor."""
    target = _target(state, cfg)
    findings = list(state.get("findings") or [])
    actor = state.get("actor")
    if actor == "external":
        return _external(state, target, findings)
    if actor == "native":
        return _native(state, cfg, target, findings)
    raise ValueError(f"work: unknown actor {actor!r}")
