"""Node contracts for the unit graph (plan sections 1.5, 1.6, 1.7, 1.11, 1.16).

One Protocol per node in state.NODE_NAMES. Each Protocol gives the node's
call signature and the state keys the node may write (also in WRITES, which
the tests check against state.UnitState). A node returns a mapping of keys to
new values; the reducer of each key then folds that value into state.

Send targets (run_check, run_verifier) receive their payload in the ``state``
argument, not the full unit state.

Routing returns one of STEP. The conditional edges of the graph call the
kernel routing function (route.next_step) and nothing else.
"""
import dataclasses
from typing import Any, Literal, Mapping, Protocol, runtime_checkable

from core.kernel.types import Spend
from core.graph.state import UnitState

# Routing steps (section 1.7, last paragraph).
STEP = Literal["work", "check", "escalate", "finalize", "end"]
STEPS = ("work", "check", "escalate", "finalize", "end")

# What a node returns: a mapping from UnitState keys to values, folded by reducers.
NodeUpdate = Mapping[str, Any]

# Keys each node may write, from the section 1.5 table and the section 1.6 keys.
WRITES: dict[str, frozenset[str]] = {
    # re-hashes surfaces, recomputes effectiveness, stale gates, status, spend
    "reconcile": frozenset({"effective", "stale", "status", "spend", "findings"}),
    # native: the agent's result summary and spend; external: the target gate
    "work": frozenset({"work_summary", "spend", "target_gate", "findings"}),
    # stores the surface digest taken before checks; clears per-attempt lists
    "check_fanout": frozenset({"surface_digest", "check_results", "verdicts"}),
    # one CheckResult, appended through collect
    "run_check": frozenset({"check_results"}),
    # fatal findings become the repair feedback
    "judge_checks": frozenset({"findings"}),
    # one VerifierVerdict, appended through collect
    "run_verifier": frozenset({"verdicts"}),
    # the vote is folded into the repair feedback when it fails
    "judge_verdicts": frozenset({"findings"}),
    # one typed human decision
    "approve": frozenset({"decisions"}),
    # the only node that appends evidence; ends the attempt on the target gate
    "seal": frozenset({"evidence", "target_gate", "findings"}),
    # increments attempts and the fail streak; clears the target gate
    "repair_route": frozenset({"attempts", "fail_streak", "target_gate"}),
    # status and streak change on retry or abandon; escalation is recorded as evidence
    "escalate": frozenset({"status", "fail_streak", "attempts", "evidence", "decisions"}),
    # the terminal status
    "finalize": frozenset({"status"}),
}


class ReconcileNode(Protocol):
    """reconcile: re-hash surfaces on disk; recompute effective, stale, status, spend."""

    def __call__(self, state: UnitState, cfg: Mapping[str, Any]) -> NodeUpdate: ...


class WorkNode(Protocol):
    """work: native actor call, or one interrupt until the runner says attempt."""

    def __call__(self, state: UnitState, cfg: Mapping[str, Any]) -> NodeUpdate: ...


class CheckFanoutNode(Protocol):
    """check_fanout: hash the gate's surface and clear the per-attempt lists."""

    def __call__(self, state: UnitState, cfg: Mapping[str, Any]) -> NodeUpdate: ...


class RunCheckNode(Protocol):
    """run_check: run one check as a subprocess; return one CheckResult."""

    def __call__(self, state: UnitState, cfg: Mapping[str, Any]) -> NodeUpdate: ...


class JudgeChecksNode(Protocol):
    """judge_checks: fold check results; fatal findings become repair feedback."""

    def __call__(self, state: UnitState, cfg: Mapping[str, Any]) -> NodeUpdate: ...


class RunVerifierNode(Protocol):
    """run_verifier: invoke the verifier subgraph with artifact and rubric text only."""

    def __call__(self, state: UnitState, cfg: Mapping[str, Any]) -> NodeUpdate: ...


class JudgeVerdictsNode(Protocol):
    """judge_verdicts: majority vote and spread; a spread above threshold escalates."""

    def __call__(self, state: UnitState, cfg: Mapping[str, Any]) -> NodeUpdate: ...


class ApproveNode(Protocol):
    """approve: one interrupt with a typed request; returns the human decision."""

    def __call__(self, state: UnitState, cfg: Mapping[str, Any]) -> NodeUpdate: ...


class SealNode(Protocol):
    """seal: re-hash the surface, refuse on mismatch, append one evidence record."""

    def __call__(self, state: UnitState, cfg: Mapping[str, Any]) -> NodeUpdate: ...


class RepairRouteNode(Protocol):
    """repair_route: count the attempt and the fail streak; route to work or escalate."""

    def __call__(self, state: UnitState, cfg: Mapping[str, Any]) -> NodeUpdate: ...


class EscalateNode(Protocol):
    """escalate: one interrupt describing why the unit is stuck; records the decision."""

    def __call__(self, state: UnitState, cfg: Mapping[str, Any]) -> NodeUpdate: ...


class FinalizeNode(Protocol):
    """finalize: set the terminal status."""

    def __call__(self, state: UnitState, cfg: Mapping[str, Any]) -> NodeUpdate: ...


# Node name to its Protocol. Every name in state.NODE_NAMES has one.
PROTOCOLS: dict[str, type] = {
    "reconcile": ReconcileNode,
    "work": WorkNode,
    "check_fanout": CheckFanoutNode,
    "run_check": RunCheckNode,
    "judge_checks": JudgeChecksNode,
    "run_verifier": RunVerifierNode,
    "judge_verdicts": JudgeVerdictsNode,
    "approve": ApproveNode,
    "seal": SealNode,
    "repair_route": RepairRouteNode,
    "escalate": EscalateNode,
    "finalize": FinalizeNode,
}


@dataclasses.dataclass(frozen=True)
class WorkResult:
    """What a work actor returns. ``summary`` and ``spend`` reach UnitState (plan 1.6).

    Contract amendment 5: ``finished`` is True when the actor ended its work step
    (for the native actor, through the ``request_gate`` tool); ``gate`` is the gate
    it asks to be checked, or None for "the first ready gate". The work node maps
    these to ``target_gate``; they are not stored in state themselves.
    """

    summary: str
    spend: Spend
    finished: bool = False
    gate: str | None = None


@runtime_checkable
class WorkActor(Protocol):
    """The native work actor (plan 1.11). Registered by name; never imported by the graph."""

    def run(self, goal: str, findings: list[dict], context: Mapping[str, Any]) -> WorkResult: ...
