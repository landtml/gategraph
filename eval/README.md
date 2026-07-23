# eval/, the template's reference corpus

- `corpus/*.yaml`, 44 synthetic cases (detection / machinery).
- `pack/`, the policy pack the corpus runs against: a mini project root
  holding `harness/adapter/adapter.yaml`.

Run `make eval` (deterministic, free). Policy, schema, split and rotation rules:
`docs/EVAL.md`.
