"""Test-time search primitives (plan §14), self-consistency aggregation.

``self_consistency`` samples the checker N times and majority-aggregates the judgements. The
**disagreement** between samples is the black-box confidence signal (§12): Anthropic's API
exposes no logprobs, so vote spread is the usable uncertainty estimate. High agreement →
proceed; disagreement above the calibrated τ → escalate rather than confidently ship.

Reflexion (critique→revise) needs no module of its own, it reuses the driver's repair loop
with the checker's critique as feedback; see ``driver._decide``. Both primitives are OFF by
default (§14: self-critique without independent evidence repeats the same error).
"""
from __future__ import annotations

from collections import Counter

# Top-level boolean judgement fields that get majority-voted.
_BOOL_FIELDS = ("gaming_clean", "structurally_promising", "exhausted")


def aggregate_judgements(judgements: "list[dict]") -> "tuple[dict, float]":
    """Majority-aggregate N checker judgements → ``(aggregated, disagreement)``.

    Booleans (top-level + each soft pass) take the majority vote; the mechanism sentence takes
    the most common string (ties → earliest seen). ``disagreement`` is the mean minority
    fraction across all voted fields: 0.0 = unanimous, → 0.5 = maximally split.
    """
    if not judgements:
        raise ValueError("aggregate_judgements needs at least one judgement")
    n = len(judgements)
    agg = dict(judgements[0])  # variant_id / notes carried from the first sample
    fractions: "list[float]" = []

    soft_keys: set = set()
    for j in judgements:
        soft_keys |= set((j.get("soft_passes") or {}).keys())
    agg_soft: dict = {}
    for key in sorted(soft_keys):
        yes = sum(1 for j in judgements
                  if bool((j.get("soft_passes") or {}).get(key, False)))
        agg_soft[key] = yes > n / 2
        fractions.append(min(yes, n - yes) / n)
    agg["soft_passes"] = agg_soft

    for key in _BOOL_FIELDS:
        yes = sum(1 for j in judgements if bool(j.get(key, False)))
        agg[key] = yes > n / 2
        fractions.append(min(yes, n - yes) / n)

    sentences = [str(j.get("mechanism_sentence", "")) for j in judgements]
    counts = Counter(sentences)
    best = max(counts, key=lambda s: (counts[s], -sentences.index(s)))
    agg["mechanism_sentence"] = best
    fractions.append(1 - counts[best] / n)

    disagreement = sum(fractions) / len(fractions) if fractions else 0.0
    return agg, disagreement
