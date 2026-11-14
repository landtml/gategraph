"""The dispatch decision table: ``next_step(state, cfg)`` (plan 1.5, 1.7; bugs B03, B20).

Pure, with no LangGraph import, so the whole table is unit-tested without a graph. The graph's
conditional edges call this and nothing else.

``state`` is a plain dict shaped like ``core.graph.state.UnitState``. It is not imported here.
``cfg`` is any object with ``gates`` (a list of ``GateSpec``). Three optional attributes are
read with ``getattr`` and ignored when absent: ``config_digest`` (the digest of the current
configuration), ``budget`` (a ``Budget``) and ``fail_streak_to_escalate`` (an int).

Precedence, first match wins:

1. Terminal or paused status: ``end``. Nothing else is consulted (B03).
2. Configuration digest differs from the one pinned at start: ``escalate``. This comes before
   finalize, so a unit is never sealed under a configuration it was not judged under.
3. A finding with rule ``SPREAD_ESCALATE_RULE`` is in ``state["findings"]``: ``escalate``. The
   blinded verifiers disagreed beyond the threshold, so the unit is not repaired (plan 1.5).
   This also comes before finalize.
4. A finding whose rule starts with ``INTEGRITY_RULE_PREFIX`` is in ``state["findings"]``:
   ``escalate``. Reconcile writes these when the evidence chain is broken or a stored surface is
   missing. Status is not changed there, so this rule is what routes the unit to escalate.
5. Every gate effective: ``finalize``. An empty gate list never finalizes.
6. Budget spent: ``escalate``.
7. Fail streak at its threshold: ``escalate``.
8. Attempt requested for a gate that is not in the configuration, or whose repair attempts
   are used up: ``escalate``.
9. Attempt requested: ``check``.
10. Otherwise: ``work``.

Standard library only; the step names duplicate ``core.graph.contracts.STEPS`` (the kernel
must not import core.graph).
"""
from __future__ import annotations

from typing import Any

from core.kernel.budget import should_stop

WORK = "work"
CHECK = "check"
ESCALATE = "escalate"
FINALIZE = "finalize"
END = "end"
STEPS = (WORK, CHECK, ESCALATE, FINALIZE, END)

# Statuses from UnitState. ``paused`` is included: a paused unit waits at an interrupt and is
# proceeded by a new run, which reconciles before it routes again.
TERMINAL_STATUSES = frozenset({"complete", "escalated", "abandoned", "paused"})

# Finding rule written by the blinded verifier node (core/graph/nodes/verify.py) when the vote
# spread is above the adapter threshold. Its presence forces escalation, never repair.
SPREAD_ESCALATE_RULE = "verifier.spread_escalate"

# Finding rules written by reconcile for integrity failures (integrity.chain_broken and
# integrity.surface_missing). Any rule with this prefix forces escalation.
INTEGRITY_RULE_PREFIX = "integrity."


def _has_rule(findings: Any, matches: Any) -> bool:
    """True when any finding's rule satisfies ``matches``. Dicts and objects both work."""
    for finding in findings or ():
        if isinstance(finding, dict):
            rule = finding.get("rule")
        else:
            rule = getattr(finding, "rule", None)
        if isinstance(rule, str) and matches(rule):
            return True
    return False


def _has_spread_escalation(findings: Any) -> bool:
    """True when any finding carries ``SPREAD_ESCALATE_RULE``."""
    return _has_rule(findings, lambda rule: rule == SPREAD_ESCALATE_RULE)


def _has_integrity_failure(findings: Any) -> bool:
    """True when any finding's rule starts with ``INTEGRITY_RULE_PREFIX``."""
    return _has_rule(findings, lambda rule: rule.startswith(INTEGRITY_RULE_PREFIX))


def next_step(state: dict, cfg: Any) -> str:
    """Return the next routing step for a unit. See the module docstring for the order."""
    if state.get("status") in TERMINAL_STATUSES:
        return END

    gates = list(getattr(cfg, "gates", None) or ())
    gate_by_id = {gate.id: gate for gate in gates}

    current_digest = getattr(cfg, "config_digest", None)
    pinned_digest = state.get("config_digest")
    if current_digest is not None and pinned_digest is not None and current_digest != pinned_digest:
        return ESCALATE

    if _has_spread_escalation(state.get("findings")):
        return ESCALATE

    if _has_integrity_failure(state.get("findings")):
        return ESCALATE

    effective = set(state.get("effective") or ())
    if gates and all(gate.id in effective for gate in gates):
        return FINALIZE

    bounds = getattr(cfg, "budget", None)
    if bounds is not None and should_stop(state.get("spend") or {}, bounds).stop:
        return ESCALATE

    streak_limit = getattr(cfg, "fail_streak_to_escalate", None)
    if streak_limit and int(state.get("fail_streak") or 0) >= int(streak_limit):
        return ESCALATE

    target = state.get("target_gate")
    if target is None:
        return WORK
    gate = gate_by_id.get(target)
    if gate is None:
        return ESCALATE
    if gate.repair is not None:
        used = int((state.get("attempts") or {}).get(target) or 0)
        if used >= gate.repair.max_attempts:
            return ESCALATE
    return CHECK
