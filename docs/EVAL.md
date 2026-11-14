# EVAL, the measurement subsystem

A single agent run varies a few percentage points even at temperature 0, so
nothing here reports a bare percentage: pass-rates carry a 95% Wilson CI,
and seed-to-seed reliability is an ICC(1). The machinery lives in `core/eval/`; the reference corpus
and the pack it runs against live in `eval/`.

The measurement code runs on the LangGraph engine.

## Entry points

- `make eval` (also `make eval-new`): the deterministic corpus, run on the new engine
  against `eval/pack_v2` and `eval/corpus`, five seeds per case. It prints pass-rate and
  CI for overall, iterate, heldout and adversarial, the ICC(1), and the gate-failure
  histogram. Detection cases run through the kernel policy; machinery cases run through
  the unit graph with stub checks. Failed trials are appended to
  the failure ledger at `harness/state/failures.jsonl` (`--root` chooses the project).
  No network, no model. The measured result on this tree: 220 of 220 passed, 44 cases
  (40 detection, 4 machinery) times 5 seeds, ICC(1) 1.000.
- `make test-eval`: the unit tests of `core/eval/`.


## Stamped projects

A stamped project has no `eval` target of its own (its Makefile has `venv`,
`check` and `maintenance`). The measurement code is shipped in `harness/core/eval/`,
and its runner takes `--root`, `--pack` and `--corpus`, so a project that wants
its own regression corpus runs `runner_v2.py` against its own pack and corpus.
That is not wired into `make check` in this build.

## Case anatomy (`eval/corpus/*.yaml`, one YAML list per file)

The corpus has 54 cases in seven files: 40 `detection`, 4 `machinery` and 10 `live`.
`core/eval/corpus.py` is the authoritative schema. Three kinds:

- `detection`: an artifact text plus `expect: {fire: [...], quiet: [...]}`
  (`quiet: ['*']` means nothing may fire). It runs the real policy engine with the
  case's repo-relative `path` for scope matching. Clean cases are the false-positive net
  and are as important as the catches.
- `machinery`: a named evidence-chain scenario (`out_of_order_refused`,
  `staleness_reopens`, `reset_invalidates_later`, `escalate_is_terminal`) run through the
  unit graph with the pack's gate chain. The scenarios test the evidence chain; the
  check commands are stubs.
- `live`: a model prompt plus `gold` checks, for a live run; `make eval` skips it.
  `gold.quiet` lists the fatal policy classes (what the harness can repair).
  `gold.require` regexes and `max_words` are requirements the harness does not enforce,
  so the A/B measures net effect, not a tautology.

Every case carries `set: iterate|heldout` and `adversarial: true|false`. Seeds
perturb detection artifacts with verdict-preserving line insertions (seed 0 is the
identity), so ICC(1) doubles as a determinism check.

**No deny-shaped literals on disk.** Write `{{ACCOUNT}}` where a case needs an
identifier-shaped value. The loader expands it at run time from a token assembled in
code, so the corpus files never contain one.

## Corpus policy (anti-Goodhart)

- **Split.** `iterate` cases are for daily tuning; `heldout` cases are never tuned
  against. If a heldout case fails, fix the harness, not the case, and reclassify a
  case only with a written rationale in the case file.
- **Rotation.** About 10% of cases per month: retire the stalest passing cases and
  seed replacements from real failures in the ledger (`harness/state/failures.jsonl`).
  Rotation is a normal commit; the corpus is versioned with the repo. The rotation
  ritual is listed by `make maintenance`, which informs and never gates.
- **Stratification.** Keep about 20% of cases adversarial or edge (`adversarial:
  true`). The printed adversarial bucket tracks that slice separately.
- **Growth loop.** Every recurring ledger failure becomes a new case and, if it is
  enforceable, a new check or deny policy in the adapter.

## The reference corpus (`eval/`)

The corpus is synthetic. The pack (`eval/pack_v2`) is a schema-2 adapter with a
five-gate chain and a set of deny and style policies, and every token in it is
invented. The corpus is both the template's own regression net (a
mutation-tested one: crippling a pack regex fails exactly its cases) and the worked
example for project corpora.

The machinery cases replace every check command with a passing stub (`true`), except
the escalation scenario, which uses a failing one (`false`) with `max_attempts: 1`.
They test the evidence chain, not the commands.
