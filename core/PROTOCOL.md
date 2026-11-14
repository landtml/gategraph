# PROTOCOL: session discipline contract

The behavioral contract for any agent session in a GateGraph project. The hooks
enforce the hard edges; this file states the discipline they assume. Commands below
run from the project root, after `make venv`:

```sh
PY=.venv/bin/python
GATE="$PY harness/core/cli/gate.py"
```

## 1. Context recovery, not re-derivation

Session start prints the active unit, its gates and the next command. Trust it.
Read `MEMORY.md` first, then open at most 2-5 files it routes you to. Never "read
everything": less-but-right context beats more.

## 2. One unit per session

Work exactly one active unit. The active pointer is `units/.active`, written by the
gate commands; a terminal unit releases it. Finishing early? Start the next unit with
`$GATE start --unit <slug>`. Stuck? `$GATE escalate --unit <slug> --note "..."`
records why. Never confidently ship wrong work, and never silently walk away.

## 3. Evidence, not opinion

- "Done" means a green `make check`, nothing else.
- A gate passes only when `$GATE attempt <gate> --unit <slug>` runs its checks and
  they pass, or when its approval is answered with `$GATE proceed`. Nothing else sets a
  gate. Do not edit a gate into a file, and do not expect `approve` or `lint --set-gate`
  to work; both are retired and refuse.
- A failing attempt prints its findings and exits 1. Read them, fix the files, attempt
  again. The findings name the rule and the path.
- Editing a file a passed gate covers makes that gate stale, and the gates that need it
  reopen. That is correct behavior, not an obstacle: attempt the stale gate again.

## 4. Pauses, approvals and requests

- `$GATE status --unit <slug>` says where the unit is and the next command. Run it
  after every change of state.
- `$GATE pending --unit <slug>` lists open requests. A work request is answered by the
  attempt itself. An approval needs a person: a `tty` approval is given from a terminal,
  and a `token` approval needs the token printed when the request was raised, which you
  must not guess or store. Do not answer a request you were not asked to answer.
- `$GATE pause --unit <slug>` leaves a unit stopped at its request. A paused unit does
  not block the session from stopping.

## 5. Commit and log

Commit at meaningful boundaries with messages that explain why. Record significant
decisions in `harness/state/decisions.jsonl` as you make them. The audit trail is
written live, not reconstructed.

## 6. Stop = consolidate

A session that touched a unit may not end with a stale handoff. Compress working state
into `HANDOFF.md` with the two required sections `## Now` and `## Next` (add
`## Decisions` and `## Gotchas` as needed), promote durable facts to
`harness/state/facts.yaml`, then stop.

The stop hook blocks an armed session while its unit is `in_progress` with a gate still
ungated, or while `HANDOFF.md` is older than the session's last unit change. The block
names the next gate command. After 3 blocks in one session the stop is allowed and one
automatic handoff stub is written. Do not rely on the stub: stubs are counted as decay.

## 7. The harness is not the enemy

If a gate or hook blocks you, the correct moves are: satisfy it (attempt the gate, answer
the request), `pause` the unit, or `escalate` with a written reason. Editing
`harness/core/`, `harness/adapter/adapter.yaml`'s policy to get past a deny rule, the
unit's `manifest.yaml` or `units/.active` to get unstuck is never correct. The guard
denies the state directory `.gategraph/` and the protected paths for that reason.
