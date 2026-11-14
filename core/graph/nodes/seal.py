"""The seal node (plan sections 1.5, 1.8; bug B18).

The only node that appends to the evidence chain. It is the one place the chain
is checked before a record is made, because the reducer is a plain append (RESULTS
Q12). On success it returns one record and clears ``target_gate``. On refusal it
returns no record and one finding, and leaves ``target_gate`` as it was, so the
graph sends the unit back through reconcile and the attempt can run again.

Refusals, in order:
- no target gate, or a target gate that the configuration does not name;
- a chain that does not pass ``verify_chain`` (the reducer no longer checks);
- a hashed gate whose surface holds no files (``seal.empty_surface``, legacy 0.4 F36):
  an approval-only gate and an unhashed gate have an empty surface by design and pass;
- a surface on disk that does not hash to ``surface_digest`` taken before the
  checks ran (bug B18); a surface that cannot be hashed is refused the same way;
- a record that ``make_record`` rejects.

The surface map is written once through ``store_surface``; the record carries
only its digest. The checkpoint identifier is never recorded: the runner binds
checkpoints from outside the graph (plan 1.8).

Clock: the ``at`` timestamp comes from ``cfg["clock"]`` when it is callable, so a
re-run on the same state gives the same record. Otherwise the current UTC time.

Standard library and core.kernel only. The node never pauses the graph.
"""
from __future__ import annotations

import datetime
import hashlib
from collections.abc import Mapping
from typing import Any

from core.graph.nodes.reconcile import cfg_value
from core.kernel.evidence import ChainError, make_record, verify_chain
from core.kernel.surface import hash_surface, store_surface
from core.kernel.types import (Actor, ActorKind, Finding, ProducerKind,
                               ProducerRecord, RecordKind, Severity, canonical_json)


def seal(state: Mapping[str, Any], cfg: Any) -> dict:
    """Return ``{"evidence": [record]}`` and clear ``target_gate``, or a refusal finding."""
    gates = list(cfg_value(cfg, "gates") or ())
    unit_dir = cfg_value(cfg, "unit_dir")
    state_dir = cfg_value(cfg, "state_dir")
    target = state.get("target_gate")

    if target is None:
        return _refuse("seal.no_target", "no gate is targeted, so nothing can be sealed")
    gate = next((g for g in gates if g.id == target), None)
    if gate is None:
        return _refuse("seal.unknown_gate", f"gate {target!r} is not in the configuration")

    chain = list(state.get("evidence") or [])
    try:
        verify_chain(chain)
    except ChainError as exc:
        return _refuse("seal.chain_broken", f"evidence chain is broken: {exc.reason}")

    try:
        surface = hash_surface(unit_dir, target, gates)
    except (ChainError, OSError, ValueError) as exc:
        return _refuse("seal.surface_unreadable", f"surface of {target!r} cannot be hashed: {exc}")
    needs_approval = bool(gate.approval is not None and gate.approval.required)
    if not surface.files and not _empty_by_design(gate, needs_approval):
        return _refuse("seal.empty_surface",
                       f"surface of {target!r} holds no files, so there is nothing to certify")
    if surface.digest() != state.get("surface_digest"):
        return _refuse("seal.surface_changed",
                       f"surface of {target!r} changed since the checks ran")
    surface_digest = store_surface(state_dir, surface)

    decisions = list(state.get("decisions") or [])
    actor = _actor(decisions, needs_approval)
    try:
        record = make_record(
            chain,
            kind=RecordKind.APPROVAL if needs_approval else RecordKind.GATE,
            gate=target,
            at=_now(cfg),
            surface_digest=surface_digest,
            producers=_producers(state, decisions, needs_approval),
            config_digest=state.get("config_digest"),
            actor=actor,
            mac_key=cfg_value(cfg, "mac_key"),
        )
    except (ChainError, TypeError, ValueError) as exc:
        return _refuse("seal.record_invalid", f"record for {target!r} is not valid: {exc}")

    return {"evidence": [record.to_dict()], "target_gate": None}


def _empty_by_design(gate: Any, needs_approval: bool) -> bool:
    """An approval-only gate, or an unhashed gate (surface include=(), config_v1), has an
    empty surface on purpose. Any other gate over zero files is refused (legacy F36)."""
    return needs_approval or (gate.surface is not None and not gate.surface.include)


def _refuse(rule: str, message: str) -> dict:
    finding = Finding(rule=rule, severity=Severity.FATAL, path=None, line=None,
                      message=message, remedy=None)
    return {"findings": [finding.to_dict()]}


def _now(cfg: Any) -> str:
    clock = cfg_value(cfg, "clock")
    if callable(clock):
        return clock()
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _digest_of(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _producers(state: Mapping[str, Any], decisions: list, needs_approval: bool) -> list:
    """One producer per check result and verifier verdict of this attempt, plus the
    approval when the gate requires one."""
    out = []
    for result in state.get("check_results") or []:
        findings = result.get("findings") or []
        out.append(ProducerRecord(
            id=result["id"], kind=ProducerKind.CHECK, digest=result["digest"],
            exit=result.get("exit"), verdict=None,
            findings_digest=_digest_of(findings) if findings else None))
    for verdict in state.get("verdicts") or []:
        findings = verdict.get("findings") or []
        out.append(ProducerRecord(
            id=verdict["id"], kind=ProducerKind.VERIFIER, digest=verdict["digest"],
            exit=None, verdict=verdict["verdict"],
            findings_digest=_digest_of(findings) if findings else None))
    if needs_approval and decisions:
        last = decisions[-1]
        out.append(ProducerRecord(
            id=last["request_id"], kind=ProducerKind.HUMAN, digest=_digest_of(last),
            exit=None, verdict=last["answer"], findings_digest=None))
    return out


def _actor(decisions: list, needs_approval: bool) -> Actor:
    if needs_approval and decisions:
        last = decisions[-1]
        return Actor(kind=ActorKind.HUMAN, id=last["by"], channel=last["channel"])
    return Actor(kind=ActorKind.TOOL, id="seal", channel=None)


__all__ = ["seal"]
