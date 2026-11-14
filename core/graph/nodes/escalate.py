"""escalate: one interrupt describing why the unit is stuck; records the decision.

Plan section 1.12 (kind ``escalation``), section 1.7 and bugs B02, B38.

Two shapes. Most escalations come after a gate attempt: ``target_gate`` and
``surface_digest`` are set, and the answers act on that gate. Some come before any
attempt, from the router: budget spent, configuration digest changed, a broken evidence
chain or missing surface (``integrity.*`` findings), or verifier spread. Those have no
target gate and no surface digest. Then ``gate`` is None in the request and in the
record, and the record's ``surface_digest`` is the digest of the empty surface
(``core.kernel.types.Surface({})``). The request id is still deterministic. The reason
is named in the payload, read from the state and from the optional ``budget`` and
``config_digest`` keys of ``cfg``.

Answers. ``retry`` reopens the unit. With a gate it resets that gate's attempt count and
the fail streak; without a gate it resets the fail streak only and leaves ``attempts``
alone. ``abandon`` and ``waive`` are terminal: a unit leaves the escalated state only
through ``retry`` (B02). ``waive`` is offered and accepted only when the adapter allows
waivers (``cfg["allow_waivers"]``) and there is a gate to waive.

Records. The decision becomes one evidence record: kind ``escalation``, or kind
``waiver`` for ``waive`` (section 1.12). Every escalation, whatever the answer,
writes one row to the failure ledger (B38). The row is written here, after the
interrupt returns. This node never runs again after it returns, so the row is
written once. The node is the only escalation writer in the graph.

Refusals. An answer outside the schema, or ``waive`` without the adapter's
permission or without a gate, raises HumanAnswerRefused. See approve.py for the
consequence.

Config keys read from ``cfg``: ``root`` (project root for the ledger, required),
``allow_waivers`` (bool), ``channel`` (request channel), ``token_digest``
(set by the command layer for the token channel), ``mac_key`` (optional), ``budget``
(core.kernel.types.Budget, optional; used only to name a budget reason) and
``config_digest`` (the current configuration digest, optional; used only to name a
configuration reason).

Standard library, the kernel, and LangGraph only.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from langgraph.types import interrupt

from core.graph.nodes.approve import (HumanAnswerRefused, _utc_now, allowed_answers,
                                      read_proceed, request_id)
from core.kernel import ledger, route
from core.kernel.budget import should_stop
from core.kernel.evidence import is_digest, make_record
from core.kernel.types import (Actor, ActorKind, HumanAnswer, HumanDecision, HumanKind,
                               HumanRequest, RecordKind, Surface)

__all__ = ["escalate", "HumanAnswerRefused"]

# Statuses the answers map to. ``waive`` routes to finalize; the status it sets is
# the terminal one (see the report: the brief does not name it).
_STATUS = {
    HumanAnswer.RETRY: "in_progress",
    HumanAnswer.ABANDON: "abandoned",
    HumanAnswer.WAIVE: "complete",
}

# Digest of the empty surface, used as the surface digest when the state has none.
EMPTY_SURFACE_DIGEST = Surface(files={}).digest()


def _rules(findings: Any) -> list[str]:
    """Rule strings of the findings. Dicts and objects both work, as in route.py."""
    rules: list[str] = []
    for finding in findings or ():
        rule = (finding.get("rule") if isinstance(finding, Mapping)
                else getattr(finding, "rule", None))
        if isinstance(rule, str):
            rules.append(rule)
    return rules


def _reasons(state: Mapping[str, Any], cfg: Mapping[str, Any]) -> list[str]:
    """Why the router may have escalated: the causes the node can see. Pure."""
    reasons: list[str] = []
    current = cfg.get("config_digest")
    if current is not None and current != state.get("config_digest"):
        reasons.append("configuration digest changed since the unit was pinned")
    bounds = cfg.get("budget")
    if bounds is not None:
        stop = should_stop(state.get("spend") or {}, bounds)
        if stop.stop:
            reasons.append(stop.reason)
    rules = _rules(state.get("findings"))
    if route.SPREAD_ESCALATE_RULE in rules:
        reasons.append("verifier votes spread beyond the threshold")
    integrity = [rule for rule in rules if rule.startswith(route.INTEGRITY_RULE_PREFIX)]
    for rule in dict.fromkeys(integrity):
        reasons.append(f"integrity finding {rule}")
    return reasons


def escalate(state: Mapping[str, Any], cfg: Mapping[str, Any]) -> dict:
    """Pause on an escalation (of the target gate, or of the unit before any attempt).

    Records the decision and the ledger row.
    """
    gate = state.get("target_gate") or None
    surface_digest = state.get("surface_digest")
    if surface_digest:
        if not is_digest(surface_digest):
            raise ValueError("escalate needs the surface digest of the gate, a sha256 hex")
    else:
        surface_digest = EMPTY_SURFACE_DIGEST
    root = cfg["root"]
    config_digest = state["config_digest"]
    kind = HumanKind.ESCALATION
    attempts = state.get("attempts") or {}
    gate_attempts = attempts.get(gate, 0) if gate else 0
    waivable = bool(cfg.get("allow_waivers", False)) and gate is not None
    allowed = allowed_answers(kind, waivable)
    reasons = _reasons(state, cfg)
    if reasons:
        reason = "; ".join(reasons)
    elif gate:
        reason = "repair attempts or fail streak reached"
    else:
        reason = "no escalation reason found in state"
    if gate:
        summary = f"gate {gate} escalated after {state.get('fail_streak', 0)} failed attempts"
    else:
        summary = f"unit escalated before any gate attempt: {reason}"
    req = HumanRequest(
        id=request_id(kind, gate, surface_digest, gate_attempts),
        kind=kind, gate=gate, channel=cfg.get("channel", "tty"),
        summary=summary, at=_utc_now(),
        payload={"gate": gate, "reason": reason,
                 "fail_streak": state.get("fail_streak", 0),
                 "attempts": gate_attempts,
                 "answers": [a.value for a in allowed],
                 "findings": list(state.get("findings") or []),
                 "verdicts": list(state.get("verdicts") or []),
                 "spend": dict(state.get("spend") or {})},
        token_digest=cfg.get("token_digest"))
    raw = interrupt(req.to_dict())
    fields = read_proceed(kind, raw, allowed)
    answer = fields["answer"]
    if answer is HumanAnswer.WAIVE and gate is None:
        raise HumanAnswerRefused("waive needs a target gate; there is nothing to waive")
    at = _utc_now()
    decision = HumanDecision(request_id=req.id, answer=answer, by=fields["by"],
                             channel=fields["channel"], note=fields["note"], at=at)
    record_kind = RecordKind.WAIVER if answer is HumanAnswer.WAIVE else RecordKind.ESCALATION
    evidence = make_record(
        state.get("evidence") or [], kind=record_kind, gate=gate, at=at,
        surface_digest=surface_digest, producers=(), config_digest=config_digest,
        actor=Actor(kind=ActorKind.HUMAN, id=fields["by"], channel=fields["channel"]),
        mac_key=cfg.get("mac_key"))
    ledger.record(root, "escalation", unit=state.get("unit"), gate=gate,
                  answer=answer.value, by=fields["by"], note=fields["note"],
                  request_id=req.id)
    update: dict[str, Any] = {
        "status": _STATUS[answer],
        "decisions": [decision.to_dict()],
        "evidence": [evidence.to_dict()],
    }
    if answer is HumanAnswer.RETRY:
        if gate:
            update["attempts"] = {**attempts, gate: 0}
        update["fail_streak"] = 0
    elif answer is HumanAnswer.ABANDON:
        update["fail_streak"] = 0
    return update
