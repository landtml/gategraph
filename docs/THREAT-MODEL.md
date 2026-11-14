# THREAT-MODEL: what the policy layer defends against, what it does not, and what `gate verify` proves

One page, written down so no project inherits this implicitly. The harness's
guardrails are **cooperative**: they exist to catch an honest-but-fallible agent
drifting off protocol, not to stop an adversary who has write access to the
project and its state directory.

## Defended: the cooperative threat model

- **Accidental writes of denied content.** Deny policies (`policy.deny` in the
  adapter) are checked before a Write or Edit (pre-write guard) and after it (the
  post-write scan), and again on every gate attempt over the gate's whole surface.
  The gate attempt is the check that counts; the hooks give early feedback.
- **Normalized matching, one function.** Pre-write, post-write and gate-time matching
  all normalize text the same way (NFKC, zero-width characters removed), so fullwidth
  digits and zero-width splits do not pass the guard and get caught only later.
  A match that spans a soft line wrap still fires.
- **Severity is respected.** A `warn` policy is advice and never blocks a write.
  An unknown severity is a configuration error, not a silent demotion.
- **Protected paths.** `protected_paths` names files the agent may not write, through
  Write, Edit, NotebookEdit and, best effort, shell commands (see below).
- **The state directory.** `.gategraph/` (checkpoints, surface maps, locks) is denied
  to any write, edit or command that names it. Only the runner writes there.
- **Gates are set by their own checks or by an answered request.** `approve` and
  `lint --set-gate` are retired and refuse. A gate changes only through
  `gate attempt` (all its checks pass) or `gate proceed` (an answered approval).
- **Stop and pause semantics.** A unit waiting for work blocks the session's stop
  until the gate is attempted. A unit waiting at an approval, an escalation or a
  pause does not block. Terminal states (complete, escalated, abandoned) do not
  reopen by `pause` or `proceed`; an escalated unit leaves that state only through the
  recorded `retry` answer.

## Not defended: the adversarial cases (by design)

The policy layer **does not defend** against a deliberately evasive writer.
Known bypass classes, listed so nobody discovers them in production:

- **Encoding.** Base64, rot13, hex, URL-encoding, or any transform a regex cannot
  see through.
- **Shell commands.** Protected-path and blind-path matching for shell commands is
  **best-effort string matching**. A command that names a protected path is denied
  when the text shows it; a command that builds the path at run time (variables,
  globbing, redirection through another tool) is not. The post-shell scan covers the
  paths the hook can attribute to the command. Everything else is caught at the next
  gate attempt, which scans the whole surface. The real protection of the manifest and
  evidence is that they are projections verified against the checkpoint (below), not
  that the shell is policed.
- **Out-of-band tools.** Content written by a tool that no hook covers is caught only
  at the next gate attempt.
- **Novel homoglyphs.** NFKC folds compatibility characters only; a Cyrillic 'о' for a
  Latin 'o' survives normalization.
- **Semantic smuggling.** Paraphrase, translation into another language, or splitting
  content across many files or commits.
- **Consistent rewrites.** Someone with write access to both the unit directory and
  `.gategraph/` can rewrite the evidence chain, the manifest, the surface maps and the
  checkpoint so that they agree. `gate verify` does not detect that (see the table
  below). The MAC step that would is not wired from the CLI.

## What `gate verify` detects

`gate verify --unit <slug>` proves one unit in five steps and stops at the first
failing step, naming it. It is read only. Its output is one line per step.

| Step | Name | What it compares | Detects | Does not detect |
|---|---|---|---|---|
| 1 | evidence chain | every record's digest and `prev` link in `evidence.jsonl` | an edited, inserted, removed or reordered record in the chain | a chain rewritten from the first record on |
| 2 | manifest head and gates | the manifest's head and gate table against the chain | a manifest that disagrees with the chain it summarizes | a manifest and chain rewritten together |
| 3 | checkpoint | the checkpoint named in the manifest against the chain record by record, and its status against the files | a checkpoint that disagrees with the chain or with the files; reports `not checked (no database)` when the database is absent | a checkpoint rewritten to match |
| 4 | surfaces | every passed gate's surface map against the files on disk now | a file that changed or was added after its gate passed (the gate is stale, not failed); a missing surface map (`surface missing`); a corrupt one (`surface corrupt`) | a change made before the gate passed |
| 5 | mac | each record's MAC, when a `mac_key` is supplied | a record altered without the key | everything, when no key is supplied |

**Step 5 is not run by the CLI in this build.** `gate verify` passes no MAC key, so it
prints `mac: not checked (no key)` for every unit. The kernel does run step 5 when a
caller supplies a key. Until the CLI supplies one, a verified unit proves that its
records, manifest, checkpoint and files agree with each other, not that nobody with
write access rewrote all of them.

A schema-1 (legacy) manifest has no evidence chain and cannot be verified. The command
says so and exits 1.

In a stamped project, `make check` runs `gate verify` over every unit that has a
manifest, and fails if any unit fails.

## Approval channels are cooperative

An approval request names its channel:

- **`tty`.** The answer is accepted only when standard input is a terminal. This keeps
  a non-interactive script out. An agent that can open a terminal can still answer.
- **`token`.** The one-time token is printed when the request is raised and is shown
  once. The request keeps only its sha256; the token is never written to a file.
  Whoever can read the output of the command that raised the request can read the
  token. The channel stops an agent that does not see that output, not one that does.

Both channels bind an answer to the request it was raised for. A wrong token is
refused with the command to run next. A work request's answer (`attempt`) is not
channel-checked. A work request is answered by the actor doing the work, which is the
point of the work step.

## Checkpoints may hold findings text

`.gategraph/checkpoints.sqlite` is the authority for a unit's graph state. Its
checkpoints hold the findings a gate produced (the rule, path, line and message of each one), the
pending requests and their payloads, and the answers given to them. In a
test project, a planted policy finding's text was found in plaintext inside the
database (in 30 stored rows of the `checkpoints` and `writes` tables). The surface maps in
`.gategraph/surfaces/` hold file paths and sha256 digests, not file content.

What exists today:

- **What is stored, exactly.** Each finding in graph state is stored as its fields:
  `rule`, `severity`, `path`, `line`, `message` and `remedy` (`core/kernel/types.py`,
  `Finding`). The `message` is the text the policy or check wrote, and it is stored as
  plain text: the standard serializer does not encrypt it, so a text search of
  `checkpoints.sqlite` finds the strings. The same applies to pending request payloads
  and answers. Remedy text and the rule id are stored the same way.
- **Encryption at rest, in the framework: not available in this build, and untested.**
  The strict-privacy serializer exists (`core/graph/store.py`), but it needs
  `pycryptodome`, which is not in `core/requirements.lock`, so the pinned environment
  cannot build it. The only test of `privacy: strict` takes the branch that expects the
  `ImportError`; the encrypted path itself has never run, and nothing checks that a
  stored message is unreadable without the key. The gate CLI always builds the
  checkpointer with the standard setting, so `privacy: strict` in the adapter does not
  take effect. Do not rely on it.
- **Encryption at rest, by the disk.** Put the project, or at least `.gategraph/`,
  on an encrypted volume. `.gitignore` in a stamped project already excludes
  `.gategraph/` from version control.
- **Pruning, by hand.** `gate archive --unit <slug>` works on a complete, escalated or
  abandoned unit. It writes the final evidence and manifest, then deletes the
  checkpoints of that unit's thread. The evidence chain and the manifest stay in the
  unit directory, and the findings text goes with the checkpoints. An archived unit
  can no longer be continued.
- **Pruning, by age.** Not implemented. There is no retention setting, and nothing
  prunes checkpoints of units that are still open.

If a unit's findings must not persist, archive it when it reaches a terminal state
and keep `.gategraph/` on an encrypted volume until then.

## Failure direction of each enforcement point

- **Pre-tool guard: fails CLOSED.** A crashing guard, an unreadable hook payload or a
  payload for the wrong event denies the write, with a message to retry. When the
  adapter cannot be loaded, the guard still allows writes under `harness/adapter/`
  (so the configuration can be repaired) and denies everything else. Denial text comes
  from `guard.denial_message`, one place.
- **Post-write scan, stop gate, session start: fail OPEN.** A crashing hook writes a
  note to standard error and exits 0, so a broken hook never bricks the session.
  Enforcement is re-earned at gate time, by `gate attempt` and by `make check`.
- **No project root found: nothing to enforce.** The hooks exit 0 with a note. A
  stamped project always has a root, so this matters only outside one.
- **A stamped project's `make check` is the load-bearing pass** (the seed Makefile).
  It runs, in order: the self-lint gating tier (stale docs, orphan references, duplicated canon, brand literals, instance
  leaks, committed state, hook registration); `gate verify` for every unit; the policy
  scan over every unit; and the kernel self-test. It does not re-run per-gate checks
  or verifiers; those run at gate attempt.

## Checks the rebuild does not have

These legacy checks have no kernel equivalent. They are gone, not renamed. Do not
read a green `make check` as covering them.

- **rule-coverage.** No check that every enforcing rule has a check behind it. The
  `enforces:` key on a deny policy is a label; nothing verifies coverage.
- **core-drift** (as a check at `make check`). `gategraph diff <dir>` still reports
  drift between a project's `harness/core/` and the template, on demand. It is not
  part of `make check`.
- **docs_lint.** The configurable doc scan (`docs_lint: {extra_globs, exclude_globs}`)
  is removed. The self-lint gating tier checks a fixed set of doc globs for make
  targets, gate names, harness paths, broken links and duplicated canon. That covers
  part of what docs_lint covered, not the same checks.
- **instance-token.** No check scans framework files for the project's instance tokens
  (for example a disclosure token). The self-lint instance-leak check takes its token
  patterns from its caller, and the command-line self-lint passes none, so it finds
  only unit slugs in framework files.

## If your project holds real secrets or PII

Bring real tooling; the template deliberately does not grow a secrets engine
(stdlib + PyYAML in the kernel). Wire a scanner as a check in the adapter, make it
fatal on your unit tree, and add a deny policy for each identifier shape that must
never reach disk.
