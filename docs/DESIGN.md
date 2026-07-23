# DESIGN, measurement evidence

This file records evidence, not claims: the method first, then the numbers as
they came out. The machinery is in `core/eval/` (`stats.py` for the Wilson
interval and ICC(1), `runner_v2.py` for the run). The corpus and the policy
pack it runs against are in `eval/` (see `docs/EVAL.md`).

## Deterministic corpus run (`make eval`)

44 synthetic cases (40 detection, 4 machinery; split into iterate and heldout
sets, about a third adversarial) run under 5 seeds of verdict-preserving
perturbation, against the policy pack in `eval/pack_v2`. Detection cases put a
text into a document and check which policy rules fire. Machinery cases walk
an evidence-chain scenario (out-of-order refusal, staleness reopen, reset,
escalation) against the real manifest code.

Result on this tree:

```
overall:      220/220 passed, 100.0% (CI 98.3%–100.0%)
adversarial:   70/70  passed, 100.0% (CI 94.8%–100.0%)
ICC(1): 1.000   gate-failure histogram: empty
```

100% is expected, because the corpus was written green against the current
pack. Its value is as a regression net: crippling one rule's regex fails
exactly the cases written for that rule, the histogram points at the gate that
ran them, the command exits 1, and the failures land in
`harness/state/failures.jsonl`. ICC(1) = 1.0 is trivial for a deterministic
system and is reported as a determinism check only.
