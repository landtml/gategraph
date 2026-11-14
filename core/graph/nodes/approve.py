"""approve: one interrupt with a typed approval request; records the human decision.

Plan section 1.12 (kind ``approval``) and the node contract in core/graph/contracts.py.
The node writes only ``decisions``. Approval channel checks (tty, token) are not
here: they live in the command layer (task G8).

Proceed value. The command layer proceeds with a mapping
``{"answer", "by", "channel", "note"}``. Anything else is refused.

Refusal raises HumanAnswerRefused. LangGraph keeps the rejected proceed value as
the pending write of the interrupt, so later proceeds on the same thread replay
it. The command layer should check the answer against the request before it
proceeds, so that a refused answer never reaches the graph.

Interrupts are deterministic. The node re-runs from its first line when it is
proceeded, so nothing before ``interrupt`` may have a side effect, and the request
id is derived from state, not drawn at random.

Standard library plus the LangGraph and kernel types it imports.
"""
from __future__ import annotations

import datetime
import hashlib
from collections.abc import Mapping
from typing import Any

from langgraph.types import interrupt

from core.kernel.types import (HUMAN_ANSWERS, HumanAnswer, HumanDecision, HumanKind,
                               HumanRequest, canonical_json)

PROCEED_KEYS = frozenset({"answer", "by", "channel", "note"})


class HumanAnswerRefused(ValueError):
    """A proceed value does not match the response schema of its request kind."""


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def request_id(kind: HumanKind, gate: str, surface_digest: Any, attempt: int) -> str:
    """Deterministic id: the same state always names the same request."""
    body = canonical_json({"kind": kind, "gate": gate,
                           "surface_digest": surface_digest, "attempt": attempt})
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]


def allowed_answers(kind: HumanKind, allow_waivers: bool = False) -> tuple[HumanAnswer, ...]:
    """The section 1.12 answers for a kind. ``waive`` only when the adapter allows it."""
    answers = HUMAN_ANSWERS[kind]
    if not allow_waivers:
        answers = tuple(a for a in answers if a is not HumanAnswer.WAIVE)
    return answers


def read_proceed(kind: HumanKind, raw: Any, allowed: tuple[HumanAnswer, ...]) -> dict:
    """Validate a proceed value against the response schema. Raises HumanAnswerRefused."""
    if not isinstance(raw, Mapping):
        raise HumanAnswerRefused("proceed value must be a mapping with answer, by, channel, note")
    extra = sorted(set(raw) - PROCEED_KEYS)
    if extra:
        raise HumanAnswerRefused(f"proceed value has unknown field(s): {', '.join(extra)}")
    try:
        answer = HumanAnswer(raw.get("answer"))
    except ValueError:
        raise HumanAnswerRefused(f"answer {raw.get('answer')!r} is not a known answer") from None
    if answer not in allowed:
        names = ", ".join(a.value for a in allowed)
        raise HumanAnswerRefused(f"answer {answer.value!r} is not allowed for {kind.value}; "
                                 f"expected one of: {names}")
    by = raw.get("by")
    if not isinstance(by, str) or not by.strip():
        raise HumanAnswerRefused("proceed value needs a non-empty 'by'")
    channel = raw.get("channel")
    if not isinstance(channel, str) or not channel.strip():
        raise HumanAnswerRefused("proceed value needs a non-empty 'channel'")
    note = raw.get("note")
    if note is not None and not isinstance(note, str):
        raise HumanAnswerRefused("'note' must be a string or null")
    if answer is HumanAnswer.REJECT and not (note or "").strip():
        raise HumanAnswerRefused("reject needs a note")
    return {"answer": answer, "by": by, "channel": channel, "note": note}


def approve(state: Mapping[str, Any], cfg: Mapping[str, Any]) -> dict:
    """Pause for an approval of the target gate; record the typed decision."""
    gate = state.get("target_gate")
    if not gate:
        raise ValueError("approve needs a target gate")
    attempts = state.get("attempts") or {}
    surface_digest = state.get("surface_digest")
    kind = HumanKind.APPROVAL
    req = HumanRequest(
        id=request_id(kind, gate, surface_digest, attempts.get(gate, 0)),
        kind=kind, gate=gate, channel=cfg.get("channel", "tty"),
        summary=f"approve gate {gate}?", at=_utc_now(),
        payload={"gate": gate, "surface_digest": surface_digest,
                 "answers": [a.value for a in allowed_answers(kind)],
                 "check_results": list(state.get("check_results") or []),
                 "verdicts": list(state.get("verdicts") or [])},
        token_digest=cfg.get("token_digest"))
    raw = interrupt(req.to_dict())
    fields = read_proceed(kind, raw, allowed_answers(kind))
    decision = HumanDecision(request_id=req.id, answer=fields["answer"], by=fields["by"],
                             channel=fields["channel"], note=fields["note"], at=_utc_now())
    return {"decisions": [decision.to_dict()]}
