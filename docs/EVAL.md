# EVAL, the measurement subsystem (§13)

A single agent run varies 2.2–6 percentage points even at temperature 0, so
nothing here reports a bare percentage: pass-rates carry a 95% Wilson CI,
seed-to-seed reliability is an ICC(1), and the A/B is a paired McNemar exact
test. The machinery lives in `core/eval/` (stamped with core); the template's
reference corpus lives in `eval/`.

## Entry points

- `make eval`, deterministic corpus × 5 seeds against the policy pack.
  Prints pass-rate+CI (overall / iterate / heldout / adversarial), ICC(1),
  and the per-gate failure histogram, the number that reveals which gates
  are theater and should be cut. Exit 1 on any case regression; failures
  are appended to `harness/state/failures.jsonl`.
- `make eval-ab`, the paired A/B on the corpus's `kind: live` cases with
  real `claude -p` workers (spends quota; never run implicitly). Arms:
  OFF = the first sketch ships; ON = fatal-policy findings are fed back for a
  bounded repair loop. Same cases × same trial indices, McNemar exact test.
  Pre-register the success criterion in `docs/DESIGN.md` BEFORE running.
- Stamped projects get `make eval` (seed Makefile); their cases live under
  `harness/eval/corpus/*.yaml` and the pack defaults to the project's own
  `adapter.yaml`.

## Case anatomy (`eval/corpus/*.yaml`, one YAML list per file)

Three kinds, see `core/eval/corpus.py` for the authoritative schema:

- `detection`, an artifact text + `expect: {fire: [...], quiet: [...]}`
  (`quiet: ['*']` = nothing may fire). Runs the real policy engine with the
  case's repo-relative `path` for scope matching. Clean cases are the
  false-positive net and are as important as the catches.
- `machinery`, a named evidence-chain scenario (`out_of_order_refused`,
  `staleness_reopens`, `reset_invalidates_later`, `escalate_is_terminal`)
  run against the real Manifest code with the pack's gate chain.
- `live`, a sketching prompt + `gold` checks, consumed only by the A/B.
  `gold.quiet` lists the fatal policy classes (what the harness can repair);
  `gold.require` regexes and `max_words` are requirements the harness does
  NOT enforce, so the A/B measures net effect, not a tautology.

Every case carries `set: iterate|heldout` and `adversarial: true|false`.
Seeds perturb detection artifacts with verdict-preserving line insertions
(seed 0 is the identity), so ICC(1) doubles as a determinism check.

**No deny-shaped literals on disk**: use the `{{ACCOUNT}}` placeholder, the
loader expands it from a runtime-assembled synthetic token.

## Corpus policy (anti-Goodhart, from §13)

- **Split.** `iterate` cases are for daily tuning; `heldout` cases are NEVER
  tuned against. If a heldout case fails, fix the harness, not the case,
  and only reclassify a case with a written rationale in the case file.
- **Rotation.** ~10% of cases per month: retire the stalest passing cases
  and seed replacements from real ledger rows (`harness/state/failures.jsonl`)
 , production failures are the best case generators. Rotation is a normal
  commit; the corpus is versioned with the repo.
- **Stratification.** Keep ~20% of cases adversarial/edge (`adversarial:
  true`); the printed adversarial bucket tracks this slice separately.
- **Growth loop.** Every recurring ledger failure becomes a new case AND
  (if enforceable) a new adapter check; generic ones get `gatebench upstream`'d.

## The template's reference corpus (`eval/`)

The corpus is synthetic. The pack (`eval/pack/harness/adapter/adapter.yaml`)
is a five-gate adapter whose every token is invented. 44 cases: 40 detection,
4 machinery; about a third adversarial. It is both the template's own regression net (a
mutation-tested one: crippling a pack regex fails exactly its cases) and the
worked example for project corpora.
