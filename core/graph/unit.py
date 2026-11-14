"""The unit graph: ``build_unit_graph(cfg)`` (plan section 1.5 diagram; brief G7; bugs B01, B03).

Wiring only. Every node is a function in core/graph/nodes/ or the builder's own small node
below; every routing decision is ``route.next_step`` or a path function that calls it.

One configuration. ``cfg`` is a mapping built by the caller. The builder turns it into one
dependency mapping, ``conf``, and gives that same mapping to every node, in the shape each node
reads: the mapping itself for nodes that read ``cfg`` (reconcile, seal, work, approve, escalate,
verify), and ``{"configurable": conf}`` for nodes that read ``config["configurable"]`` (the check
nodes). Nodes are never edited for this.

Required ``cfg`` keys: ``config`` (core.kernel.config.Config), ``unit_dir``, ``state_dir``,
``config_digest`` (the digest pinned in state at start), ``budget`` (core.kernel.types.Budget;
refused when unbounded). Optional: ``checkpointer`` (default in-memory), ``policies``,
``pdf_command``, ``channel``, ``token_digest``, ``allow_waivers``, ``mac_key``, ``clock``,
``load_artifact``, ``load_rubric``, ``model_ports``, ``spread_threshold``, ``work_actor``,
``goals``, ``fail_streak_to_escalate``.

Builder-owned pieces (no file in core/graph/nodes/):

* ``repair_route`` counts the attempt and the fail streak and clears ``target_gate``.
* ``finalize`` sets the terminal status.
* Reconcile is wrapped to count ``spend.iterations`` and ``spend.elapsed_s``. The node does not
  update spend (see its docstring), and the budget backstops need it.
* Seal is wrapped to refuse a target whose ``needs`` are not effective (out-of-order, legacy
  machinery scenario). Seal itself does not check ``needs``.

Routing. Every conditional edge calls ``route.next_step`` or a function that does. The exhaustion
rule of ``next_step`` only fires while ``target_gate`` is set, and ``repair_route`` clears it. So a
failure is judged before repair: the path function projects the attempt count and fail streak
that repair would write, and asks ``next_step`` with the target still set. ``ESCALATE`` goes to
``escalate``; anything else goes to ``repair_route``.

Every ``ESCALATE`` from ``route.next_step`` goes to the ``escalate`` node, with or without a target
gate. The node names the reason (budget, configuration digest, integrity, spread) from state and
from the optional ``budget`` and ``config_digest`` keys. With no target gate it records the decision
against the empty surface digest.

A work step that returns a ``work.*`` fatal finding (an unfinished native actor, an unknown gate,
a variant fault) is a failed attempt. The builder's work wrapper names the gate the step aimed at
as ``target_gate`` when the work node did not, and ``after_work`` judges the failure with the same
``failure`` path a failed check uses. So ``attempts`` and ``fail_streak`` grow and the unit cannot
loop ``work`` -> ``reconcile`` -> ``work`` past the repair bounds.

Imports: core.graph, core.kernel and LangGraph only. No product name appears here.
"""
from __future__ import annotations

import datetime as _dt
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from core.graph.nodes.approve import approve
from core.graph.nodes.check import (check_fanout, fan_out_checks, has_fatal_finding,
                                    judge_checks, run_check)
from core.graph.nodes.escalate import escalate
from core.graph.nodes.reconcile import reconcile
from core.graph.nodes.seal import seal
from core.graph.nodes.verify import judge_verdicts, run_verifier, verify_fanout
from core.graph.nodes.work import work
from core.graph.state import NODE_NAMES, UnitState
from core.graph.store import make_checkpointer
from core.kernel import policy as kpolicy
from core.kernel import route
from core.kernel.budget import refuse_unbounded
from core.kernel.faults import is_retryable
from core.kernel.types import Finding, Severity, Spend

SEAL_NEEDS_RULE = "seal.needs_not_effective"


class UnknownUnit(RuntimeError):
    """The thread holds no unit: it was never started. Proceeding with it must not create one (B39)."""

_TERMINAL_KEEP = ("abandoned", "complete")


def initial_state(*, project: str, unit: str, config_digest: str, actor: str = "external") -> dict:
    """The state a new thread starts from. The runner passes this as the first input."""
    return {
        "project": project, "unit": unit, "actor": actor, "config_digest": config_digest,
        "status": "in_progress", "effective": [], "stale": [], "target_gate": None,
        "surface_digest": None, "evidence": [], "check_results": [], "verdicts": [],
        "findings": [], "attempts": {}, "fail_streak": 0, "spend": Spend().to_dict(),
        "decisions": [], "work_summary": None,
    }


def _dependencies(cfg: Mapping[str, Any]) -> dict:
    """The one configuration every node reads. See the module docstring for the keys."""
    base = cfg["config"]
    conf: dict[str, Any] = {
        "config": base,                       # check nodes: .root, .checks, .gates
        "root": base.root,                    # escalate: ledger root
        "gates": list(base.gates),            # reconcile, seal, work, verify, approve
        "unit_dir": Path(cfg["unit_dir"]),
        "state_dir": Path(cfg["state_dir"]),
        "policies": (cfg["policies"] if cfg.get("policies") is not None
                     else kpolicy.compile_policies(base)),
        "pdf_command": cfg.get("pdf_command"),
        "channel": cfg.get("channel", "tty"),
        "token_digest": cfg.get("token_digest"),
        "allow_waivers": cfg.get("allow_waivers", False),
        "mac_key": cfg.get("mac_key"),
        "clock": cfg.get("clock"),
        "load_artifact": cfg.get("load_artifact"),
        "load_rubric": cfg.get("load_rubric"),
        "model_ports": cfg.get("model_ports") or {},
        "spread_threshold": cfg.get("spread_threshold", 0.0),
        "goals": cfg.get("goals") or {},
        "budget": cfg["budget"],                 # escalate: names a budget reason only
        "config_digest": cfg["config_digest"],   # escalate: names a configuration reason only
    }
    if cfg.get("work_actor") is not None:
        conf["work_actor"] = cfg["work_actor"]
    return conf


def _now(conf: Mapping[str, Any]) -> _dt.datetime:
    clock = conf.get("clock")
    if callable(clock):
        return _dt.datetime.fromisoformat(clock())
    return _dt.datetime.now(_dt.timezone.utc)


WORK_RULE_PREFIX = "work."


def _work_failed(findings: Any) -> bool:
    """True when a fatal finding of a work step (rule ``work.*``) is in the list."""
    return any(isinstance(f, Mapping) and f.get("severity") == Severity.FATAL.value
               and str(f.get("rule", "")).startswith(WORK_RULE_PREFIX)
               for f in (findings or ()))


def build_unit_graph(cfg: Mapping[str, Any]):
    """Compile the unit graph for one configuration. See the module docstring."""
    budget = cfg["budget"]
    refuse_unbounded(budget)
    conf = _dependencies(cfg)
    base = conf["config"]
    runnable = {"configurable": conf}
    gates = {gate.id: gate for gate in base.gates}
    # route.next_step reads gates, config_digest, budget, fail_streak_to_escalate by attribute.
    route_cfg = _RouteView(gates=tuple(base.gates), config_digest=cfg["config_digest"],
                           budget=budget,
                           fail_streak_to_escalate=cfg.get("fail_streak_to_escalate"))

    def _first_ready(state) -> str | None:
        """The first gate in declared order that is not effective and whose needs all are."""
        effective = set(state.get("effective") or ())
        for gate in base.gates:
            if gate.id not in effective and all(n in effective for n in gate.needs):
                return gate.id
        return None

    # ---- builder-owned nodes -------------------------------------------------

    def work_node(state):
        """The work node, plus the attempt bookkeeping for a failed native step.

        The native path replaces ``findings`` with this step's own findings, so a finding from an
        earlier step cannot be judged again. A failed step with no target gets the gate it aimed
        at, so ``failure`` can count it.
        """
        aimed = state.get("target_gate") or _first_ready(state)
        update = dict(work(state, conf))
        if state.get("actor") == "native":
            update["findings"] = list(update.get("findings") or [])
        if _work_failed(update.get("findings")) and not update.get("target_gate"):
            update["target_gate"] = aimed
        return update

    def reconcile_node(state):
        if not state.get("project") or not state.get("unit"):
            raise UnknownUnit("this thread has no unit; start it with initial_state (B39)")
        update = dict(reconcile(state, conf))
        now = _now(conf)
        spend = Spend(**(state.get("spend") or {})).to_dict()
        started = spend.get("first_started_at") or now.isoformat()
        elapsed = max(0.0, (now - _dt.datetime.fromisoformat(started)).total_seconds())
        spend.update(iterations=int(spend.get("iterations") or 0) + 1,
                     first_started_at=started, elapsed_s=elapsed)
        update["spend"] = Spend(**spend).to_dict()
        return update

    def seal_node(state):
        target = state.get("target_gate")
        gate = gates.get(target)
        if gate is not None:
            effective = set(state.get("effective") or ())
            missing = [need for need in gate.needs if need not in effective]
            if missing:
                finding = Finding(rule=SEAL_NEEDS_RULE, severity=Severity.FATAL, path=None,
                                  line=None, remedy=None,
                                  message=f"gate {target} needs {missing}, which are not effective")
                return {"findings": [finding.to_dict()]}
        return seal(state, conf)

    def repair_node(state):
        gate = _target(state)
        attempts = dict(state.get("attempts") or {})
        attempts[gate] = int(attempts.get(gate) or 0) + 1
        return {"attempts": attempts,
                "fail_streak": int(state.get("fail_streak") or 0) + 1,
                "target_gate": None}

    def finalize_node(state):
        status = state.get("status")
        if status in _TERMINAL_KEEP:
            return {"status": status}
        effective = set(state.get("effective") or ())
        if gates and all(gate_id in effective for gate_id in gates):
            return {"status": "complete"}
        return {"status": "escalated"}

    # ---- routing -------------------------------------------------------------

    def dispatch(state):
        step = route.next_step(state, route_cfg)
        if step == route.END:
            return END
        if step == route.WORK:
            return "work"
        if step == route.CHECK:
            return "check_fanout"
        if step == route.FINALIZE:
            return "finalize"
        return "escalate"

    def failure(state) -> str:
        """Judge a failed attempt before repair clears target_gate (see the docstring)."""
        gate = _target(state)
        attempts = dict(state.get("attempts") or {})
        attempts[gate] = int(attempts.get(gate) or 0) + 1
        projected = {**state, "attempts": attempts,
                     "fail_streak": int(state.get("fail_streak") or 0) + 1}
        return "escalate" if route.next_step(projected, route_cfg) == route.ESCALATE else "repair_route"

    def approval_or_seal(state) -> str:
        gate = gates.get(state.get("target_gate"))
        if gate is not None and gate.approval is not None and gate.approval.required:
            return "approve"
        return "seal"

    def after_checks(state):
        if has_fatal_finding(state.get("findings")):
            return failure(state)
        gate = gates.get(state.get("target_gate"))
        if gate is not None and gate.verifiers:
            return verify_fanout(state, conf)          # list of Send("run_verifier", ...)
        return approval_or_seal(state)

    def after_verdicts(state):
        if has_fatal_finding(state.get("findings")):
            return failure(state)
        return approval_or_seal(state)

    def after_approve(state):
        decisions = state.get("decisions") or []
        if decisions and decisions[-1].get("answer") == "approve":
            return "seal"
        return failure(state)

    def after_work(state):
        # Only work.* fatal findings of this step count (the wrapper clears older ones).
        if _work_failed(state.get("findings")):
            return failure(state)
        return "reconcile"

    def after_seal(state):
        # Success clears target_gate. A refusal leaves it set and adds one fatal finding.
        if state.get("target_gate"):
            return failure(state)
        return "reconcile"

    def after_escalate(state):
        return "reconcile" if state.get("status") == "in_progress" else "finalize"

    # ---- assembly ------------------------------------------------------------

    nodes = {
        "reconcile": reconcile_node,
        "work": work_node,
        "check_fanout": lambda state: check_fanout(state, runnable),
        "run_check": lambda state: run_check(state, runnable),
        "judge_checks": lambda state: judge_checks(state, runnable),
        "run_verifier": lambda state: run_verifier(state, conf),
        "judge_verdicts": lambda state: judge_verdicts(state, conf),
        "approve": lambda state: approve(state, conf),
        "seal": seal_node,
        "repair_route": repair_node,
        "escalate": lambda state: escalate(state, conf),
        "finalize": finalize_node,
    }
    retry = {"run_verifier", "work"}

    graph = StateGraph(UnitState)
    for name in NODE_NAMES:
        if name in retry:
            graph.add_node(name, nodes[name], retry_policy=RetryPolicy(retry_on=is_retryable))
        else:
            graph.add_node(name, nodes[name])

    graph.add_edge(START, "reconcile")
    graph.add_conditional_edges("reconcile", dispatch,
                                ["work", "check_fanout", "finalize", "escalate", END])
    graph.add_conditional_edges("work", after_work, ["reconcile", "repair_route", "escalate"])
    graph.add_conditional_edges("check_fanout", lambda state: fan_out_checks(state, runnable),
                                ["run_check"])
    graph.add_edge("run_check", "judge_checks")
    graph.add_conditional_edges("judge_checks", after_checks,
                                ["repair_route", "escalate", "approve", "seal", "run_verifier"])
    graph.add_edge("run_verifier", "judge_verdicts")
    graph.add_conditional_edges("judge_verdicts", after_verdicts,
                                ["repair_route", "escalate", "approve", "seal"])
    graph.add_conditional_edges("approve", after_approve, ["seal", "repair_route", "escalate"])
    graph.add_conditional_edges("seal", after_seal, ["reconcile", "repair_route", "escalate"])
    graph.add_edge("repair_route", "work")
    graph.add_conditional_edges("escalate", after_escalate, ["reconcile", "finalize"])
    graph.add_edge("finalize", END)

    checkpointer = cfg.get("checkpointer")
    if checkpointer is None:
        checkpointer = make_checkpointer({"in_memory": True})
    return graph.compile(checkpointer=checkpointer)


def _target(state: Mapping[str, Any]) -> str:
    gate = state.get("target_gate")
    if not gate:
        raise RuntimeError("the unit graph reached an attempt step without a target gate")
    return gate


class _RouteView:
    """The attributes route.next_step reads from its cfg argument."""

    def __init__(self, **attrs: Any) -> None:
        self.__dict__.update(attrs)


__all__ = ["SEAL_NEEDS_RULE", "UnknownUnit", "build_unit_graph", "initial_state"]
