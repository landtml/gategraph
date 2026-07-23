# Changelog

## 0.4.0-dev

- `gategraph init --retrofit` adds the framework to an existing repository without
  overwriting files; `--defaults` accepts every default.
- Adapter config edits go through validated commands (`add-check`, `set-gate-checks`).
- Adapter checks that run inside hooks share one time budget
  (`GATEGRAPH_HOOK_CHECK_BUDGET`, 75 s by default).
- Adding files to a hashed gate's surface reopens that gate.
- A per-release upgrade test covers the sync path (`gategraph update`).

## 0.3.0

- The measurement subsystem in `core/eval/`: Wilson intervals, ICC(1) and McNemar's
  exact test; a versioned case corpus with iterate and heldout sets and seeded,
  verdict-preserving perturbations; a failure ledger.

## 0.2.0

- First retrofit of an existing project onto the stamped core, with the core fixes it
  surfaced promoted upstream through `gategraph upstream`.

## 0.1.0

- The stamper (`init`, `update`, `diff`, `upstream`, `gen`), a config-driven evidence
  chain, the gate CLI, Claude Code hooks, the self-lint and the session memory model.
