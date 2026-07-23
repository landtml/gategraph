"""Run-level halting + the variant-level drift heuristic + config sanity.

The always-on tripwire lives in the gate/decision path (it voids a single variant). This
module is about *bounding the run*: stop on the graduate target (success) or when a backstop
is exhausted; back out of a single variant that has turned into a rabbit hole; and, the
refuse-if-unbounded rule (plan §8.4), never start a run whose three backstops are all null
unless the config says ``i_understand_unbounded: true`` explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class StopDecision:
    stop: bool
    reason: str


def should_stop(
    state: dict,
    *,
    graduate_target: int,
    max_iterations: int | None = None,
    cost_budget_usd: float | None = None,
    wall_clock_minutes: float | None = None,
) -> StopDecision:
    # Success first, reaching the graduate target beats every other bound.
    if state.get("graduated", 0) >= graduate_target:
        return StopDecision(True, f"success: {graduate_target} findings graduated")
    if max_iterations is not None and state.get("iterations", 0) >= max_iterations:
        return StopDecision(True, f"backstop: iteration cap {max_iterations} reached")
    if cost_budget_usd is not None and state.get("cost_usd", 0.0) >= cost_budget_usd:
        return StopDecision(True, f"backstop: cost budget ${cost_budget_usd} exhausted")
    if wall_clock_minutes is not None and state.get("elapsed_minutes", 0.0) >= wall_clock_minutes:
        return StopDecision(True, f"backstop: wall-clock {wall_clock_minutes} min reached")
    return StopDecision(False, "continue")


def is_drifting(variant: dict, *, max_attempts: int, max_minutes: float) -> bool:
    """True when one variant has consumed too many re-iterations or too much time."""
    return variant.get("attempts", 0) > max_attempts or variant.get("minutes", 0.0) > max_minutes


def validate_loop_config(loop: dict) -> "list[str]":
    """Structural sanity for the ``loop:`` section. Returns human-readable problems.

    The load-bearing rule: a run with ``max_iterations``, ``cost_budget_usd`` AND
    ``wall_clock_minutes`` all null is unbounded, refuse unless the config carries an
    explicit ``i_understand_unbounded: true``.
    """
    problems: "list[str]" = []
    bk = loop.get("backstops") or {}
    bounds = (bk.get("max_iterations"), bk.get("cost_budget_usd"),
              bk.get("wall_clock_minutes"))
    if all(b is None for b in bounds) and not loop.get("i_understand_unbounded"):
        problems.append(
            "all three backstops (max_iterations, cost_budget_usd, wall_clock_minutes) "
            "are null, set at least one, or set i_understand_unbounded: true")
    if loop.get("enabled"):
        if not str(loop.get("oracle") or "").strip():
            problems.append("loop.enabled is true but loop.oracle is empty")
        sc = loop.get("self_consistency") or {}
        if sc.get("enabled") and int(sc.get("n") or 0) < 1:
            problems.append("self_consistency.n must be >= 1 when enabled")
        rf = loop.get("reflexion") or {}
        if rf.get("enabled") and int(rf.get("max_iters") or 0) < 1:
            problems.append("reflexion.max_iters must be >= 1 when enabled")
    return problems
