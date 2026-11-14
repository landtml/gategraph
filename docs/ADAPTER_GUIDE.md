# ADAPTER GUIDE

A per-project adapter is the project's own enforcement. It is the single source
of truth for which gates a unit passes through, which checks earn each gate and
which write-time policy applies. The harness (`harness/core/`) never changes it.

In this guide `<harness>` stands for the harness directory of a stamped project,
which is `harness` in a project's root.

A stamped project's adapter has these parts:

- **(a)** `harness/adapter/adapter.yaml`, schema 2. The schema is
  `core/kernel/schema/adapter.v2.schema.yaml`; `core/kernel/schema/examples/adapters/`
  holds worked examples. The loader (`core/kernel/config.py`) reports every
  problem it finds, naming the key.
- **(b)** `harness/adapter/checks/*.py` and any other command a check runs, the
  executable checks.
- **(c)** `harness/adapter/review_rubric.md`, the rubric a verifier scores against
  (used only by gates that declare verifiers).
- **(d)** the project section of `CLAUDE.md`, instructional rules. Anything that can
  be enforced belongs in `policy:` or a check, not in prose.
- **(e)** `.claude/skills/<project>/workflow.md`, the one canonical workflow. Its gate
  table is generated; see "Generated blocks" below.

## adapter.yaml, schema 2

```yaml
schema: 2                      # required, must be 2
project: "my-project"          # required, a slug
unit_noun: "unit"              # required, what one unit of work is (a slug)
unit_dir: "units"              # required, where units live, relative to the root
packs: []                      # optional, domain packs stamped to packs/<name>/

checks:                        # optional, named checks that gates refer to
  - {id: policy, command: "python3 <harness>/adapter/checks/policy_scan.py --unit {unit}", severity: fatal}

gates:                         # required, at least one, in chain order
  - {id: composed, checks: [policy]}
  - {id: checked, checks: [policy]}
  - {id: verified, checks: [policy], seals_unit: true}

policy:                        # optional, the inputs of the guard (see below)
  deny:
    - {id: acct, regex: '\d{6}\s*-?\s*\d{4}', scope: [any], severity: fatal,
       summary: "no account number on disk", enforces: no-acct}
  ask: []
  blind: []
  protected_paths: ["units/*/manifest.yaml"]
```

### checks

Each entry under `checks:` has `id` (a slug), `command` (a shell-free command
line, run from the project root), `severity` (`fatal` or `warn`), and optional
`timeout_s` and `on` (a list of globs; the check runs only when a changed path
matches).

- `{unit}` in the command is replaced with the unit's directory, shell-quoted.
- `{pack}` is replaced with `packs/<name>` for a check that a pack declares.
- Quote glob keys in YAML as `'on':` when you write them by hand. A bare `on:` is a
  YAML 1.1 boolean in some parsers; the loader reads YAML 1.2 and keeps the key, but
  the quoted form is safe everywhere.

**Exit-code contract.** `0` pass, `1` fatal, `2` warn. Any other exit (127, a
signal, a crash, a timeout) is "the check did not run", which is fatal. A gate is
never earned on a check that did not run. `severity: warn` on the check entry makes
its exit-1 findings advisory. A check may print one JSON object per finding on
standard output (`rule`, `severity`, `path`, `line`, `message`, optional `remedy`);
the exit code still decides.

Default timeout per check is 120 s. In the post-write hook, every check shares one
wall-clock budget, 75 s by default, set with `GATEGRAPH_HOOK_CHECK_BUDGET`. A check
that cannot start inside the budget is skipped with an explicit advisory naming
`make check`. Anything slow belongs in a gate attempt or in `make check`, never
behind a per-write hook.

### gates

Each gate has:

- `id` (a slug), unique.
- `needs` (optional): the gates that must pass first. Default: the previous gate in
  the list.
- `checks` (optional): names from `checks:`.
- `verifiers` (optional): a list of `{rubric, n, model}`. `rubric` is a path, `n` is
  the number of independent calls (at least 1), `model` a model name. Verifiers are
  implemented and tested in the kernel and graph; the gate CLI supplies no model
  ports in this build, so a gate with verifiers cannot be earned from `gate attempt`.
- `approval` (optional): `{required: true, channel: tty}` or `channel: token`. The
  gate waits for a human answer through `gate proceed`. See the threat model for
  what each channel protects.
- `surface` (optional): `{include: [globs], exclude: [globs]}`, relative to the unit.
  Default: every file in the unit except bookkeeping. The surface is the set of files
  whose digests the gate's evidence records. Files that a later gate lists in its
  `produces` are excluded from this gate's surface.
- `produces` (optional): globs for files a later gate writes.
- `repair` (optional): `{max_attempts: N}`. Without it the router does not cap
  attempts at that gate, so set it to bound repairs.
- `seals_unit` (optional): true on the last gate, which ends the unit's chain.

A gate with neither checks, verifiers nor approval is a configuration error. A
gate with an empty surface is not refused: it passes over whatever files exist,
so put a check on the files that matter.

### policy

The `policy:` block is optional as a whole. It holds the inputs to
`core/kernel/guard.py`:

- `deny`: `{id, regex, scope, severity, summary, enforces, pretool}`. The regex must
  compile. Text is normalized (NFKC, zero-width characters removed) before matching,
  the same as the scanner. `severity: warn` never blocks a write. `pretool: false`
  keeps the rule out of the pre-write guard and leaves it to the post-write scan.
  `enforces` names the rule the policy is for (a label; nothing checks coverage).
- `ask`: `[{paths, reason}]`, paths where a write needs a human to allow it.
- `protected_paths`: globs the agent may not write. Shell commands that name one
  are denied too, on a best-effort basis.
- `blind`: paths the work step may not read or execute (for example rubrics, gold
  files, verifier code). A command that names one is denied, best effort. Empty by
  default.

Packs carry no policy. The state directory (`.gategraph/`) is protected by a built-in
rule that the adapter cannot relax: a write, edit or command that names it is denied.

## Editing the adapter

Use the stamper for targeted edits; it keeps the comments in `adapter.yaml`:

```sh
python3 gategraph.py add-check <dir> --id <id> --cmd "<command>" --gate <gate> [--on "<glob>"] [--severity fatal|warn]
python3 gategraph.py set-gate-checks <dir> <gate> <check> [<check> ...]
```

Both regenerate the generated doc blocks in the same step, so the workflow doc
cannot drift from the adapter. `add-check` prints the gate command to run next.

Hand edits are fine. After one, run `python3 gategraph.py gen <dir>` to regenerate
the generated blocks, and `make check` to validate the adapter.

The earlier commands `set-gate-command` and the `--enforces` flag are removed. A
gate's command is always `gate attempt <gate> --unit <slug>`; there is no per-gate
command to set. `--enforces` is replaced by the `enforces:` key in a deny policy.

## Gate-setting rules

- A gate passes only by `gate attempt` (its checks, all passing) or by answering its
  approval request through `gate proceed`.
- `gate verify` proves the chain of each unit after the fact (see the threat model).
- Hand-setting a gate in a file is not a way to pass it. The `approve` command and
  `lint --set-gate` are retired and refuse with a pointer to the right command.

## Oracle (search only)

The stamped project does **not** get an oracle file. The template keeps
`seed/oracle.py.tmpl`, a skeleton of the `Oracle` subclass that the variant-search
graph uses. To use search, copy the template into the project, implement `run_gates`
(it is the only required method), and pass it as `--oracle MOD:CLS` to
`core/cli/search.py`. The module maps to a file under the project root, with
`harness/` as a fallback: `--oracle adapter.oracle:MyOracle` loads
`<harness>/adapter/oracle.py`. Without `--oracle` the search uses
`core.kernel.toy_oracle:ToyOracle`, a toy with no real project behind it.

The search command runs with `--mock` only in this build: a scripted stand-in model
that never calls a provider. A real-model search is not wired.

## Generated blocks

Derived doc content lives between two HTML comments that carry the tag
`gategraph:gen`: one opens the block and names it, one closes it. The content is rendered by `gategraph gen` from the adapter. Edit
the adapter, never the block. The legacy markers `fh:gen` and `fh:auto-handoff` are
read for compatibility and never written.

The stamped `workflow.md` ships without a gate-table block. The seed's gate-table
block is not yet in the template (see the CHANGELOG known gaps), so a stamped
project's gate table is written by hand until it lands.

## Archived units are not imported

A version-1 unit whose manifest sits under an `archive/` directory of the unit
directory is not imported by `migrate`, and nothing imports it later. This is a limit
of this build, not a feature: the lazy import on first command was not built.

- `migrate` lists each such unit in its plan (step 6) and records it in the snapshot as
  read-only with status `archived`. The plan line says "imported read-only and
  lazily", but no thread is created and no evidence is written. Read that line as "left
  alone".
- No gate command imports an archived unit on first use. It has no checkpoint in the
  graph, so the gate commands cannot read its state, and `gate verify` does not check
  its chain.
- Its `manifest.yaml` and `evidence.jsonl` stay on disk. They are history, not a live
  gate: nothing in the new system enforces them.

How to handle one:

1. Default: leave it archived. Keep the directory and its files as they are, and say in
   the project's notes that the unit was finished under the version-1 gates.
2. If the unit must be gated again, move its directory out of `archive/` to the top
   level of the unit directory, then run `python3 gategraph.py migrate <dir>` on a copy
   of the project first. The dry run shows whether the unit would be imported. Migrate
   imports only top-level version-1 units, and re-importing one into a project that is
   already migrated has no test.

`gate archive` is a different thing: it deletes the checkpoints of a finished unit in
the graph (see the threat model). It does not move a unit into an `archive/` directory.

## Retired from schema 1

| Schema 1 key or command | Schema 2 |
|---|---|
| `workflow.gates`, `gate_commands` | `gates:` with `checks:`; the command is always `gate attempt` |
| `policies.deny`, `policies.ask`, top-level `protected_paths` | `policy:` block |
| `escalation.*`, `loop.*`, `self_consistency.*` (autonomous loop) | removed; the native agent loop has its own budget (`max_iterations`, `max_model_calls`) in code |
| `docs_lint:` | removed; see the lost checks in the threat model |
| `set-gate-command`, `--enforces` | removed; see "Editing the adapter" |

Schema 1 files are still read through a translator (`core/kernel/config_v1.py`),
so an existing adapter loads. New adapters are written in schema 2.
