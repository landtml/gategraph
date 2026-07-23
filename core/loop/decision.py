"""The deterministic 3-state verdict.

The blinded checker supplies *judgements*, did the soft passes hold, is the variant
structurally promising, is the mechanism objectively exhausted, what is the one-sentence
mechanism, is there gaming. This function maps those + the locked gate result onto
GRADUATE / PROBATION / REJECT with **no discretion of its own**, so the bar is auditable and
cannot drift:

  * tripwire breach or detected gaming                              → REJECT (the run is void)
  * every pass AND a clear mechanism                                → GRADUATE
  * >= probation bar, structurally promising, not exhausted         → PROBATION (re-queue)
  * otherwise (exhausted, an informative kill, or below the bar)    → REJECT

Thresholds are parameter-free by design, not a config knob someone could quietly loosen:
GRADUATE needs every pass (oracle passes + soft passes); the probation bar is all-but-two.
Config-level thresholds belong in ``loop.gates.metric_floors``, not here.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.loop.schemas import GateResult, Verdict

# The soft (checker-judged) passes, in target-selection order. Fixed by CHECKER_CONTRACT.
SOFT_PASSES = ["mechanism_lead", "robustness"]


@dataclass
class DecisionOutcome:
    verdict: Verdict
    n_passed: int
    target_pass: str | None   # PROBATION: the first failing pass to attack on re-iteration
    reason: str


def _first_failing(gate: GateResult, soft_passes: dict, has_mechanism: bool) -> str | None:
    for name in gate.passes:                 # oracle passes, in the oracle's declared order
        if not gate.passes.get(name, False):
            return name
    for name in SOFT_PASSES:
        if not soft_passes.get(name, False):
            return name
    if not has_mechanism:
        return "mechanism"
    return None


def decide(
    gate: GateResult,
    *,
    soft_passes: dict,
    gaming_clean: bool,
    mechanism_sentence: str,
    structurally_promising: bool,
    exhausted: bool,
) -> DecisionOutcome:
    total = len(gate.passes) + len(SOFT_PASSES)
    graduate_min = total
    probation_min = max(total - 2, 1)

    n_passed = gate.n_passed + sum(1 for p in SOFT_PASSES if soft_passes.get(p, False))
    has_mechanism = bool(mechanism_sentence and mechanism_sentence.strip())
    target = _first_failing(gate, soft_passes, has_mechanism)

    # Hard vetoes first, these void the variant regardless of the scoreboard.
    if not gate.tripwire_ok:
        return DecisionOutcome(Verdict.REJECT, n_passed, None,
                               "tripwire: safety audit fired")
    if not gaming_clean:
        return DecisionOutcome(Verdict.REJECT, n_passed, None,
                               "gaming: checker flagged metric-gaming/cherry-picking")

    if n_passed >= graduate_min and has_mechanism:
        return DecisionOutcome(Verdict.GRADUATE, n_passed, None,
                               f"{n_passed}/{total} + clear mechanism")

    if n_passed >= probation_min and structurally_promising and not exhausted:
        return DecisionOutcome(Verdict.PROBATION, n_passed, target,
                               f"promising at {n_passed}/{total}; re-iterate on {target}")

    reason = "objectively exhausted" if exhausted \
        else f"below bar ({n_passed}/{total}, not promising)"
    return DecisionOutcome(Verdict.REJECT, n_passed, None, reason)
