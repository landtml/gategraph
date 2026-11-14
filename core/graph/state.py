"""Graph state for the unit graph and the variant-search graph.

Representation choice. State values are plain JSON. Records held in
state are therefore the ``to_dict()`` form of the C1 types in
core/kernel/types.py (EvidenceRecord, CheckResult, VerifierVerdict, Finding,
HumanDecision, Spend), annotated here as ``dict`` with a comment naming the C1
type. No TypedDict mirrors are kept: core/kernel/types.py is the single source
of truth for their shape.

Reducers. Every reducer in this module is total. It accepts any input, never
raises and never validates; validation belongs in nodes (plan 1.6, RESULTS Q12).
A list-like ``new`` is appended; any other non-None value is wrapped as a
one-item list; ``None`` is treated as an empty list.

This module imports no graph stack and no kernel module.
"""
from typing import Annotated, Any, Literal, TypedDict


def _as_list(value: Any) -> list:
    """Coerce a reducer input to a fresh list. Never raises."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def append_evidence(old: Any, new: Any) -> list:
    """Plain append of evidence records. Never raises.

    Chain rules (``seq`` continues the list, ``prev`` equals the last digest)
    are enforced by the seal node before it returns a record, and re-checked by
    reconcile through evidence.verify_chain. The reducer does not check them.
    """
    return _as_list(old) + _as_list(new)


def append_records(old: Any, new: Any) -> list:
    """Plain append for decisions and other append-only logs. Never raises."""
    return _as_list(old) + _as_list(new)


def collect(old: Any, new: Any) -> list:
    """Append, and clear on a ``None`` update (plan 1.6).

    ``check_fanout`` writes ``None`` to clear the per-attempt lists when an
    attempt starts. Parallel ``run_check`` branches append to the same key.
    """
    if new is None:
        return []
    return _as_list(old) + _as_list(new)


class UnitState(TypedDict, total=False):
    """The unit graph state, exactly the 18 keys of plan section 1.6."""

    # identity, set by the runner at start and never changed
    project: str
    unit: str
    actor: Literal["native", "external"]
    config_digest: str

    # the evidence chain; each item is EvidenceRecord.to_dict() (no surface map)
    evidence: Annotated[list[dict], append_evidence]

    # where we are (recomputed by reconcile on every pass)
    status: Literal["in_progress", "paused", "escalated", "abandoned", "complete"]
    effective: list[str]
    stale: list[str]
    target_gate: str | None
    surface_digest: str | None

    # one attempt's results; cleared when an attempt starts
    check_results: Annotated[list[dict], collect]   # CheckResult.to_dict()
    verdicts: Annotated[list[dict], collect]        # VerifierVerdict.to_dict()
    findings: list[dict]                            # Finding.to_dict()

    # bounds
    attempts: dict[str, int]
    fail_streak: int
    spend: dict                                     # Spend.to_dict()

    # humans
    decisions: Annotated[list[dict], append_records]  # HumanDecision.to_dict()

    # work
    work_summary: str | None


class SearchState(TypedDict, total=False):
    """The variant-search state, derived from the search contract.

    The dead-end registry is a JSONL event log, not state, so it does not
    appear here. Three named counters replace the old shared attempts counter
    (1.14, bug B22). Spend holds iterations and elapsed_s as well (Spend).
    """

    project: str
    brief_digest: str
    status: Literal["in_progress", "paused", "escalated", "abandoned", "complete"]
    stop_reason: str | None                 # the named backstop that halted the run
    variants: Annotated[list[dict], collect]          # this iteration's results
    proposals: Annotated[list[dict], append_records]    # recorded after smoke passes
    repairs: int
    reproposals: int
    revisions: int
    fail_streak: int
    spend: dict                             # Spend.to_dict()
    decisions: Annotated[list[dict], append_records]


# The node names of plan section 1.5 that are added with add_node. Dispatch and
# the other routing functions are listed in ROUTING_NAMES, not here.
NODE_NAMES: tuple[str, ...] = (
    "reconcile",
    "work",
    "check_fanout",
    "run_check",
    "judge_checks",
    "run_verifier",
    "judge_verdicts",
    "approve",
    "seal",
    "repair_route",
    "escalate",
    "finalize",
)

# Pure routing functions of section 1.5. They read state and return the next
# node or a list of Send objects; they write nothing.
ROUTING_NAMES: tuple[str, ...] = (
    "dispatch",
    "approval_route",
    "verify_fanout",
)
