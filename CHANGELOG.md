# Changelog

## 0.5.0-dev

The engine is rebuilt on LangGraph.

### Breaking changes

- Adapters use schema 2 (`harness/adapter/adapter.yaml`). Schema-1 adapters are still
  read through a translator (`core/kernel/config_v1.py`), and `gategraph migrate` moves a
  0.4 project to the new layout (see [docs/MIGRATION.md](docs/MIGRATION.md)).
- Units run on the `gate` command (`harness/core/cli/gate.py`): `start`, `status`,
  `attempt`, `pending`, `proceed`, `pause`, `abandon`, `escalate`, `verify`, `archive`.
- A gate passes only through `gate attempt` (its checks pass) or an answered approval
  (`gate proceed`).
- `set-gate-command` and `add-check --enforces` are removed. Use
  `add-check --gate <gate>` and `set-gate-checks <dir> <gate> <check>...`.
- Runtime state lives in `.gategraph/` (`checkpoints.sqlite`, `surfaces/`, `locks/`).
- A failing `gate attempt` prints its findings and exits 1.
- The pre-tool hook fails closed on malformed input.
- The search command replaces the old autonomous loop.

### Added

- Approval channels: `tty` (the answer must come from a terminal) and `token` (a
  one-time token, stored only as its sha256).
- The adapter `policy:` block: `deny`, `ask`, `protected_paths` and `blind`, enforced by
  one guard function before every tool call.
- A hash-linked evidence chain per unit, checked by `gate verify`.
- Staleness: editing a certified file reopens the gates that depend on it.
- A native agent loop with guard and budget middleware, and agent tools with declared
  effects, including tools served over MCP.
- Optional blinded verifiers that score an artifact against a rubric.
- Domain packs: `code`, `document`, `research`, `data` and `ops`.
- The deterministic corpus eval on the new engine (`make eval`, 220 of 220).

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
