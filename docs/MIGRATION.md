# MIGRATION: moving a stamped project to the schema-2 layout

This is the procedure for migrating one existing project, stamped from an earlier template
version, to the layout this template now writes. It is written for the owner of that project.
Migrating a real project is the owner's action; the template never does it.

The command is `gategraph.py migrate`. It is a dry run by default. With `--apply` it performs
the steps and makes one git commit that holds only the paths it wrote. It undoes itself if a
step fails.

Every command below uses two shell variables. Set them once:

```
GB=/path/to/the/gategraph/template     # the template checkout that has `migrate`
PROJ=/path/to/your/project             # the project being migrated
```

Placeholders in angle brackets are yours to fill: `<slug>` is a unit name, `<archive-dir>` is
the directory that holds finished units, `<pre-sha>` is the commit before the migration.

## What this rehearsal does and does not cover

Read this before you start. The migration was rehearsed on a fixture, not on your project.

- The rehearsal (`tests/test_migrate_rehearsal.py`) builds a fixture from the `legacy-0.4` tag
  and stamps it `0.4.0-dev`, which is what that tag's VERSION file says. It has three units in
  different states, a hashed gate with real files, one local core edit, two version-1 adapter
  checks and one archived unit. The migration runs on it, every invariant of the plan is checked,
  and the migration is undone with `git revert` plus the state backup.
- The fixture's local core edit makes its own `make check` fail before any migration, by design.
  The rehearsal therefore checks that the rollback gives the same result as before, not a pass.
- The plan names a `v0.2.0` tag. No such tag exists. A project stamped from 0.2.0 is therefore
  not covered by the rehearsal. The dry run in step 1 is the check for your version.
- The rehearsal covers one archived unit. Archived units are imported read-only and are not
  imported lazily: the runner refuses their slugs, so `gate verify` cannot check them. Step 5
  checks an archived unit by byte comparison with the commit from before the migration.
- The fixture's adapter has two checks. A real adapter may import core internals directly, or
  call `Manifest.set_gate` from its own scripts. The report lists those; read it (step 2).

## Before you start

1. **Stop active sessions in the project.** Close any agent session that works in it. The
   migration reads every unit and rewrites some of them; a session that edits a unit at the same
   time is a risk the migration does not check for.
2. **Start from a clean tree and a known commit.**
   ```
   git -C "$PROJ" status --porcelain        # must print nothing
   git -C "$PROJ" rev-parse HEAD            # write this down: it is <pre-sha>
   ```
   A dirty tree is refused. `--allow-dirty` exists, but then the commit holds only the
   migration's paths and your uncommitted work stays uncommitted; commit or stash instead.
3. **Make a backup outside the tree.** `harness/state/` is git-ignored, so git does not hold it.
   The migration takes its own copy of `harness/state/` at step 1, but keep one of your own:
   ```
   git -C "$PROJ" bundle create "$HOME/<project>-pre-migrate.bundle" --all
   tar -C "$PROJ" -czf "$HOME/<project>-state-pre-migrate.tgz" harness/state
   ```
4. **Record the gates of every unit before anything changes.** Run the legacy command on the old
   layout, before any migration step.
   ```
   cd "$PROJ"
   python3 harness/core/gate.py status --unit <slug>  > "$HOME/<project>-status-before-<slug>.txt"
   ```
   Repeat for every unit directory under `notes/` except the archive directory. Keep these
   files outside the project. They are the reference for the rollback.
   Also record the result of your own check, whatever it is:
   ```
   cd "$PROJ" && make -s check > "$HOME/<project>-make-check-before.txt" 2>&1; echo "exit=$?"
   ```
   A project whose check already fails before the migration will still fail after a rollback.
   What matters is that the rollback gives the same result.
5. **Check the template you run.** It must be the template that has the migrate command. It
   reports its own version, and the project's stamp must not be newer than it:
   ```
   cat "$GB/VERSION"
   ```
6. **Python and network.** Use the template's Python (3.11 or newer). Step 5 of the migration
   builds the project's virtual environment with `pip install --require-hashes` from the lock
   file, so it needs network access the first time. If you have no network, the migration
   stops after step 4 with the tree consistent (see "If the run is refused or rolled back" below).

## Step 1: dry run

```
python3 "$GB/gategraph.py" migrate "$PROJ"
```

Expect exit 0 and the line `dry run: nothing is written`. Then check that nothing changed:

```
git -C "$PROJ" status --porcelain        # still prints nothing
git -C "$PROJ" rev-parse HEAD            # still <pre-sha>
```

Read the first lines of the output:

- `step 1: stamped vX, template vY; tree clean`. If the template is older than the stamp, the
  command refuses with `newer than this template`. Use the newer template.
- `step 1: legacy status for N unit(s)`. Compare each unit's `status` and `gates passed` with
  the files you saved in "Before you start". They must agree. A unit listed as `archived` is
  finished and read-only; it is not imported.

If the dry run exits non-zero, nothing was written. Read the `REFUSED` line and fix that cause.

## Step 2: read the report

The dry run lists what apply will do. Read every line; each one is a decision you may need to
make. The lines you are looking for:

- **`step 2: ... differs from its stamp; kept as <name>.local`**. A core file was edited in your
  project. Apply keeps your version beside the new one as `<name>.local` and names the seam that
  replaces it (for a second host, the seam is `core/hosts/bridge.py` and a key in the shim). Decide
  whether the edit is still needed. If the seam covers it, port the edit to the seam after the
  migration and then delete the `.local` file. If it is not covered, keep the `.local` file until
  you have ported the edit. Do not delete it first: that loses the edit.
- **`step 3: dropped key '<key>' (no schema-2 field)`**. These keys have no field in the new
  adapter. The escalation thresholds, the state-reaping patterns, the loop settings, the doctor
  block and the framework directories are all dropped this way. For each dropped key, decide
  whether the behaviour it controlled still matters. The original adapter is kept as
  `harness/adapter/adapter.v1.yaml`; nothing is lost from git.
- **`step 3: gate <g> was approvable by hand: approval.required`**. A gate you used to approve
  by hand now needs an answer through the runner, in a terminal (`gate proceed`). Expect this at
  the gates the report names.
- **`step 3: gate <g>: <old command> (retired) ...`**. A hashed gate that was set by a lint
  command is now the policy check plus the adapter checks it names. Read the check list the
  report prints.
- **`step 4: hook registration now runs the shim: <Event>: <script>`**. The four hook file
  names do not change. Only their path moves into `harness/core/hosts/shims/`.
- **`step 7: Makefile line <n> names a legacy path ...`**. The migration never edits your
  Makefile. These lines name paths the shims still serve. They are a list for you to tidy, not
  a failure.

Also note any adapter check whose script calls `Manifest.set_gate` directly. The migrated
gate keeps its old command; the shim lets that command attest a pass through the runner. Plan
to turn such scripts into ordinary checks before the shims are removed (see "The deprecation
window").

## Step 3: apply on a branch

Apply only on a branch, so the merge is a separate decision:

```
git -C "$PROJ" switch -c gategraph-migrate
python3 "$GB/gategraph.py" migrate "$PROJ" --apply
```

Expect exit 0 and, at the end, `commit: one commit <sha>`. The run prints one `step 8` line per
unit and `effective gates identical` for each. If any effective gate differs from the step 1
snapshot, the run rolls itself back and exits 1; see "If the run is refused or rolled back".

Then check the commit:

```
git -C "$PROJ" show --stat HEAD | tail -1
git -C "$PROJ" show --name-only --format= HEAD | sort
```

The changed paths should be the ones the report named: `harness/` (core, adapter, hosts,
`harness/.fh/MANIFEST.json`), `.claude/settings.json`, `.gitignore`, `notes/<slug>/manifest.yaml`,
`notes/<slug>/evidence.jsonl` and `notes/.active` (removed; the runner keeps its own active
pointer), and `Makefile.v2` only if the report says it was written. Nothing else. Check the
archive is unchanged:

```
git -C "$PROJ" diff --quiet <pre-sha> HEAD -- <archive-dir> && echo "archive unchanged"
```

## Step 4: verify the migrated project

Run each check and compare with what you expect. All commands run inside the project.

1. **The project's own check.** Its Makefile is unchanged, so this is still your `make check`:
   ```
   cd "$PROJ" && make -s check; echo "exit=$?"     # expect exit=0
   ```
   If the migration wrote `Makefile.v2`, its `check` did not run. Adopt `Makefile.v2` by hand
   (copy its targets into your Makefile, or point your CI at it) and run `make -f Makefile.v2 check`.
2. **Every unit verifies.** The project's venv runs the new runner:
   ```
   cd "$PROJ"
   .venv/bin/python harness/core/cli/gate.py verify --unit <slug>     # expect exit 0
   ```
   Repeat for every migrated unit (not the archive).
3. **The gates match the dry run.** For each unit:
   ```
   .venv/bin/python harness/core/cli/gate.py status --unit <slug>
   ```
   The `gates passed:` line must equal the one you saved in "Before you start".
4. **The legacy command names need the venv now.** After migration, `python3 harness/core/gate.py`
   forwards some verbs (`status`, for one) to the new runner, which needs LangGraph. Under bare
   `python3` those fail with `ModuleNotFoundError: No module named 'langgraph'`. Use
   `.venv/bin/python harness/core/gate.py <verb>` instead. The hooks are different: they run under
   the system `python3` and need only the kernel and PyYAML, so check that
   `python3 -c "import yaml"` works.
5. **The adapter loads.** `harness/adapter/adapter.yaml` now has `schema: 2`. The version-1
   adapter is kept as `harness/adapter/adapter.v1.yaml` for reference.
6. **Your version-1 check scripts still run.** Run each one with the command the adapter names,
   from the project root. They must exit 0. A script that calls a shim function writes a
   deprecation line to `harness/logs/hooks.log`; a script that only imports the shim module does
   not. Count the lines after the scripts have run:
   ```
   grep -c "deprecated version-1 shim" "$PROJ/harness/logs/hooks.log"
   ```
   A line is written once per shim function per process. A script that calls a shim adds one
   line each time it runs.
7. **The hooks run.** Start one session in the project and do one real edit. Then check that the
   stop and post-tool hooks ran and that `harness/logs/hooks.log` has no traceback.

## Step 5: walk one new unit, and check an archived unit

This is the last check before you merge. A new unit must go through every gate with the new
runner, and an archived unit must be unchanged.

1. **Walk a new unit.** Give it a new slug, then follow the runner's `next:` line at each step:
   ```
   .venv/bin/python harness/core/cli/gate.py start --unit <new-slug>
   .venv/bin/python harness/core/cli/gate.py attempt <gate> --unit <new-slug>
   .venv/bin/python harness/core/cli/gate.py pending --unit <new-slug>
   .venv/bin/python harness/core/cli/gate.py proceed --unit <new-slug> --decision approve --by <you>
   ```
   An approval needs a terminal on standard input; it cannot come from a script. Continue until
   `status` shows the unit complete.
2. **Check an archived unit.** The runner does not verify archived units, so this check is the
   byte comparison from step 3 of this runbook, repeated after your session:
   ```
   git -C "$PROJ" diff --quiet <pre-sha> HEAD -- <archive-dir> && echo "archive unchanged"
   git -C "$PROJ" status --porcelain -- <archive-dir>        # expect nothing
   ```
   This is weaker than the plan's wording ("still verifies"). It proves the archive is the bytes
   you had before. It does not re-check the archive's gates.

## Step 6: merge

Merge the branch by your usual review. Nothing in the migration merges for you.

```
git -C "$PROJ" switch <your-main-branch>
git -C "$PROJ" merge --no-ff gategraph-migrate
```

## If the run is refused or rolled back

- **`REFUSED: the working tree is dirty`**. Nothing was written. Commit or stash, then run again.
- **`REFUSED: ... newer than this template`**. Nothing was written. Use the newer template.
- **`ROLLED BACK: ...`** and exit 1. The migration put back every path it had touched and the
  state directories from its backup. Nothing was committed. Read the message; it names the step
  that failed. Check with `git -C "$PROJ" status --porcelain` (expect nothing) and
  `git -C "$PROJ" rev-parse HEAD` (expect `<pre-sha>`).
- **`stopped after step 4`** and exit 0. The core and adapter are migrated and committed, and the
  units are still in their old form, waiting for the environment. The usual cause is that the
  venv could not be built, most often because there was no network. Once network is available,
  run the apply again. It builds the project venv itself from the lock file, then finishes
  steps 5 to 8:
  ```
  python3 "$GB/gategraph.py" migrate "$PROJ" --apply
  ```
  The migration prints a `make venv` hint. Do not follow it unless your project's Makefile has
  that target. The template has it; your project's Makefile is never edited, so it usually does
  not.

## Rollback

There are two cases. Decide which one you are in.

### Before the merge: throw the branch away

Nothing outside the branch changed, so this is the cleanest undo:

```
git -C "$PROJ" switch <your-original-branch>
git -C "$PROJ" branch -D gategraph-migrate
```

Then restore the state from your backup if you want to be sure:

```
rm -rf "$PROJ/harness/state"
tar -C "$PROJ" -xzf "$HOME/<project>-state-pre-migrate.tgz"
```

The migration created `.gategraph/` (git-ignored). Remove it if it did not exist before:

```
rm -rf "$PROJ/.gategraph"
```

Deleting the branch discards the migration commit. Nothing else in git is rewritten.

### After the merge: revert

One commit undoes the migration, and the state backup is copied back. Do this only if no unit
has been worked on with the new runner since the migration. The runner keeps its state in
`.gategraph/`, which git does not hold, so reverting cannot restore that state for new units.

1. Find the migration commit: `git -C "$PROJ" log --oneline --grep "gategraph migrate"`.
2. Revert it, which adds one new commit and does not rewrite history:
   ```
   git -C "$PROJ" revert --no-edit <migration-sha>
   ```
3. Restore the state directory from the migration's backup. The backup is a sibling of the
   project, named `<project>.migrate-backup-<UTC stamp>`, and holds `state/`:
   ```
   B=$(ls -d "$(dirname "$PROJ")/$(basename "$PROJ").migrate-backup-"* | tail -1)
   rm -rf "$PROJ/harness/state" && cp -a "$B/state" "$PROJ/harness/state"
   ```
   The backup holds a `.gategraph/` copy only if that directory existed before the migration.
   Restore it if the backup has one; otherwise remove the project's `.gategraph/`:
   ```
   rm -rf "$PROJ/.gategraph"
   [ -d "$B/.gategraph" ] && cp -a "$B/.gategraph" "$PROJ/.gategraph"
   ```
4. Check the legacy layout is back. Compare with the files you saved in "Before you start":
   ```
   cd "$PROJ"
   python3 harness/core/gate.py status --unit <slug>        # must equal the saved text
   make -s check > "$HOME/<project>-make-check-after.txt" 2>&1; echo "exit=$?"
   ```
   The `status` output must match exactly. The `make check` exit code must equal the one you
   recorded before the migration. Its output may differ only in timestamps or counts you expect.

The revert in step 2 creates a commit, so git needs an identity. If `git revert` fails with
"Please tell me who you are", set `user.name` and `user.email` in the project and run it again.

Keep the backup directory and your bundle until you have used the migrated project for a while.
Delete them only after you are sure you will not need to roll back.

## The deprecation window for the shims

The old top-level module names (`harness/core/gate.py`, `lint.py`, `manifest.py` and so on) are
shims over `core/compat/`. The four hook scripts are served by shims in
`harness/core/hosts/shims/`. The shims are not permanent. Calls to the version-1 functions they
wrap write one line to `harness/logs/hooks.log`, once per function per process:

```
deprecated version-1 shim: <name> called by <script>; use <replacement>; removed one minor release after the migration command ships
```

The rule is that the shims are removed one minor release after the migration command ships. From
that release on, your project must have:

- no adapter check or gate command that calls `Manifest.set_gate` or the other shim functions
  (the log line names each one);
- a `make check` that runs the new paths (the Makefile line list from step 7 of the report);
- hook registrations that point at `harness/core/hosts/shims/` (already the case after migration).

Watch the log: `grep "deprecated version-1 shim" "$PROJ/harness/logs/hooks.log"`. When your
scripts stop adding lines, they no longer call the shims.

`harness/adapter/adapter.v1.yaml` is kept for reference. Delete it once you no longer need it.
