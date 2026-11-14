"""Budgets: the refuse-if-unbounded rule and the spend backstops (plan 1.14, bug B19).

Pure functions over a ``Budget`` (types.py) and a spend record. ``spend`` may be a
``Spend`` or its ``to_dict()`` form, which is what graph state holds. Elapsed time lives in
the spend, so it survives a restart (B19); nothing here reads a clock.

Standard library only.
"""
from __future__ import annotations

import dataclasses
from typing import Any

from core.kernel.types import Budget, Spend


class UnboundedBudget(ValueError):
    """A run was asked to start with no backstop and no explicit opt-in."""


@dataclasses.dataclass(frozen=True)
class StopDecision:
    stop: bool
    reason: str


# The three backstops that must not all be null (plan 1.14; backstops.validate_loop_config):
# cost, iteration and wall-clock.
_BACKSTOP_FIELDS = ("max_usd", "max_iterations", "max_wall_s")


def refuse_unbounded(bounds: Budget) -> None:
    """Raise UnboundedBudget when all three backstops are null and the opt-in is not set."""
    if bounds.allow_unbounded:
        return
    if all(getattr(bounds, name) is None for name in _BACKSTOP_FIELDS):
        raise UnboundedBudget(
            "all three backstops (max_usd, max_iterations, max_wall_s) are null; "
            "set at least one, or set allow_unbounded: true explicitly")


def _spend_dict(spend: Any) -> dict:
    if isinstance(spend, Spend):
        return spend.to_dict()
    if isinstance(spend, dict):
        return spend
    raise TypeError(f"spend must be a Spend or a dict, got {type(spend).__name__}")


# (spend key, Budget field, reason word). Checked in this order; the first exhausted wins.
_BOUNDS = (
    ("usd", "max_usd", "cost"),
    ("tokens", "max_tokens", "token"),
    ("model_calls", "max_model_calls", "model-call"),
    ("tool_calls", "max_tool_calls", "tool-call"),
    ("elapsed_s", "max_wall_s", "wall-clock"),
    ("iterations", "max_iterations", "iteration"),
)


def should_stop(spend: Spend | dict, bounds: Budget) -> StopDecision:
    """Stop when any bound that is set has been reached (>=). Unset bounds are ignored."""
    current = _spend_dict(spend)
    for spend_key, field, word in _BOUNDS:
        limit = getattr(bounds, field)
        if limit is None:
            continue
        used = current.get(spend_key) or 0
        if used >= limit:
            return StopDecision(True, f"backstop: {word} budget {limit} reached ({used} spent)")
    return StopDecision(False, "continue")
