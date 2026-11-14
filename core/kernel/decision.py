"""The deterministic 3-state verdict (kernel port of the legacy decision module).

The blinded checker supplies judgements: did the soft passes hold, is the variant structurally
promising, is the mechanism objectively exhausted, what is the one-sentence mechanism, is there
gaming. This function maps those plus the locked oracle result onto GRADUATE / PROBATION /
REJECT with no discretion of its own, so the bar is auditable and cannot drift:

  * tripwire breach or detected gaming                              -> REJECT (the run is void)
  * every pass AND a clear mechanism                                -> GRADUATE
  * at or above the probation bar, structurally promising, not exhausted -> PROBATION (re-queue)
  * otherwise (exhausted, an informative kill, or below the bar)    -> REJECT

The soft-pass names are read from ``oracle.soft_passes()``; the core never hard-codes them.
Thresholds are parameter-free by design. Config-level thresholds belong in ``floors``.

Standard library only.
"""
from __future__ import annotations

from dataclasses import dataclass

from core.kernel.oracle import VariantVerdict, Oracle, OracleGateResult


@dataclass
class DecisionOutcome:
    verdict: VariantVerdict
    n_passed: int
    target_pass: "str | None"   # PROBATION: the first failing pass to attack on re-iteration
    reason: str


def _first_failing(gate: OracleGateResult, soft_names: "list[str]", soft_passes: dict,
                   has_mechanism: bool) -> "str | None":
    for name in gate.passes:                 # oracle passes, in the oracle's declared order
        if not gate.passes.get(name, False):
            return name
    for name in soft_names:                  # soft passes, in the oracle's declared order
        if not soft_passes.get(name, False):
            return name
    if not has_mechanism:
        return "mechanism"
    return None


def decide(
    gate: OracleGateResult,
    *,
    oracle: Oracle,
    soft_passes: dict,
    gaming_clean: bool,
    mechanism_sentence: str,
    structurally_promising: bool,
    exhausted: bool,
) -> DecisionOutcome:
    soft_names = list(oracle.soft_passes())
    total = len(gate.passes) + len(soft_names)
    graduate_min = total
    probation_min = max(total - 2, 1)

    n_passed = gate.n_passed + sum(1 for p in soft_names if soft_passes.get(p, False))
    has_mechanism = bool(mechanism_sentence and mechanism_sentence.strip())
    target = _first_failing(gate, soft_names, soft_passes, has_mechanism)

    # Hard vetoes first: these void the variant regardless of the scoreboard.
    if not gate.tripwire_ok:
        return DecisionOutcome(VariantVerdict.REJECT, n_passed, None,
                               "tripwire: safety audit fired")
    if not gaming_clean:
        return DecisionOutcome(VariantVerdict.REJECT, n_passed, None,
                               "gaming: checker flagged metric-gaming/cherry-picking")

    if n_passed >= graduate_min and has_mechanism:
        return DecisionOutcome(VariantVerdict.GRADUATE, n_passed, None,
                               f"{n_passed}/{total} + clear mechanism")

    if n_passed >= probation_min and structurally_promising and not exhausted:
        return DecisionOutcome(VariantVerdict.PROBATION, n_passed, target,
                               f"promising at {n_passed}/{total}; re-iterate on {target}")

    reason = "objectively exhausted" if exhausted \
        else f"below bar ({n_passed}/{total}, not promising)"
    return DecisionOutcome(VariantVerdict.REJECT, n_passed, None, reason)
