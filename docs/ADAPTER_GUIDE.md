# ADAPTER GUIDE

A per-project adapter is exactly five things (plan §6):

- **(a)** `harness/adapter/adapter.yaml`, single source of truth for
  enforcement. The stamped file carries
  inline comments for every key.
  Schema notes (v0.4.0): `escalation.disagreement_threshold` is the τ
  vote-spread cut-point (a RAW disagreement fraction, not an error
  probability; the pre-0.4 name `error_prob_threshold` is still read for
  one release). `escalation.infra_retry_limit` bounds consecutive infra
  faults before the loop stops escalated. The loop's SUCCESS condition is
  `loop.stop.graduate_target` (unset = 9999, i.e. backstops end the run);
  `loop.backstops.rate_limit_guard` is the optional usage-window guard,
  both are shown commented in the seed. Removed dead keys:
  `workflow.terminal_states`, `escalation.confidence_signal`,
  `self_consistency.aggregate` (majority is the implementation).
- **(b)** `harness/adapter/checks/*.py`, executable checks. Exit-code
  contract: `0` pass / `1` fatal / `2` warn; ANY other exit (127 command
  not found, signals, crashes) is fatal "check did not run": a gate is
  never earned on a check that never executed (v0.4.0+). A `severity: warn`
  on the `checks:` entry demotes its exit-1 findings to advisory. Keep
  each ≤ ~100 LOC.
  **Editing config (v0.4.0+):** register checks with
  `gatebench add-check <dir> --id X --cmd "..." --on "glob"` and rewrite gate
  commands with `gatebench set-gate-command <dir> <gate> "<cmd>"`, both are
  targeted text edits (adapter.yaml's inline comments survive, unlike a
  yaml round-trip) and regenerate the gatebench:gen doc blocks in the same step,
  so dup-canon can never be left broken (F1/F8). `--enforces` is refused
  unless the `<!-- rule:… -->` annotation is already live. Inside a
  stamped project (no gatebench), regenerate with `python3 harness/core/gen.py`.
  **Reference pattern:** every stamp ships
  `harness/adapter/checks/verify_claims.py`, a REQUIRED-claims check
  that fails on a claim's ABSENCE, not just on refuted values (a typo'd
  or deleted `claim:` line must fail the gate, not skip verification,
  F6).
  In `checks:` entries, ALWAYS quote the glob key as `'on':`, bare `on:`
  is a YAML 1.1 boolean and config.py rejects it (v0.2.0+; before that the
  check silently never ran). Optional top-level `docs_lint:
  {extra_globs, exclude_globs}` widens/prunes the living-doc scan used by
  harness_lint's doc tiers and `lint.py refs` (v0.2.0+).
  **Check time budgets (v0.4.0+):** in the PostToolUse hook context all
  checks share one wall-clock budget (default 75s, `FH_HOOK_CHECK_BUDGET`
  env) sitting under the hook's 90s timeout in `.claude/settings.json`; a
  check that can't start within it is SKIPPED with an explicit advisory
  naming `make check`. Outside hooks (gate-earning `lint.py --set-gate`,
  `make check`) there is no budget, each check gets the full
  `CHECK_TIMEOUT` (120s). Anything slower belongs in `make check` or a
  gate producer, never behind a per-write hook.
- **(c)** `harness/adapter/oracle.py`, Layer 2 only; subclasses the
  `Oracle` ABC. `run_gates(variant, cfg)` is the ONLY required method
  (v0.4.0+). Optional overrides: `proposal_space()`, a non-empty list
  becomes the maker prompt's allowed `cell` vocabulary (keeps exact-match
  dedup meaningful); `incumbent()`, never called by the engine, available
  to project checks.
  **CONTRACT block (v0.4.0+):** the stamped skeleton opens with an explicit
  `CONTRACT` dict (evaluation `domain` + `boundary_probes`). run_gates must
  sample from it, boundary probes included, and any variant guide must
  print it rather than restate it: the consumer-D consumer graduated a
  variant that violated its written contract because the oracle grid
  never crossed a documented boundary (F3).
  **registry.jsonl contract:** an EVENT log (`kind:` proposal /
  gate_result / verdict; the LATEST verdict per variant wins), NOT
  one-line-per-iteration, never count raw lines; consume via the
  `Registry` reduction API (`events()`, `graduated_ids()`,
  `probation_ids()`, `dead_end_cells()`). Same note in registry.py's
  docstring.
- **(d)** the project section of `CLAUDE.md`, instructional rules. Anything
  enforceable must be promoted into adapter.yaml and tagged with a
  `rule:<id>` annotation matched by a check's `enforces: <id>`.
- **(e)** `.claude/skills/<project>/workflow.md`, the one canonical
  workflow; `SKILL.md` only points to it; gate tables are `gatebench gen` blocks.

Grow the harness with the project: every observed failure becomes a check
here; generic checks get promoted to the template with `gatebench upstream`.
