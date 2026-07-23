# PROTOCOL: session discipline contract

The behavioral contract for any agent session in a Gatebench project.
The hooks enforce the hard edges; this file states the discipline they assume.

## 1. Context recovery, not re-derivation

Session start prints the active unit, gates, and next command. Trust it.
Read `MEMORY.md` first, then open at most 2-5 files it routes you to.
Never "read everything": less-but-right context beats more.

## 2. One unit per session

Work exactly one active unit (`<unit_dir>/.active`, owned by gate.py).
Finishing early? Start the next unit. Stuck? `gate.py escalate --note "..."`.
Never confidently ship wrong work, and never silently walk away.

## 3. Evidence, not opinion

- "Done" means a green `make check`, nothing else.
- Gates advance only via their `gate_commands` entry; tool-set gates are
  earned from their producing check, never claimed.
- Editing an artifact after verification reopens its hashed gates. That is
  correct behavior, not an obstacle: re-earn them.

## 4. Commit + log

Commit at meaningful boundaries with messages that explain why. Append
significant decisions to the episodic log (`harness/state/decisions.jsonl`)
as you make them: the audit trail is written live, not reconstructed.

## 5. Stop = consolidate

A session that touched the unit may not end with a stale handoff. Compress
working state into `HANDOFF.md` (now / next / decisions / gotchas), promote
durable facts to `harness/state/facts.yaml`, then stop. The stop gate blocks
until this is done (and after 3 blocks writes a fallback stub; do not rely
on it, stubs are counted as decay).

## 6. The harness is not the enemy

If a gate or hook blocks you, the correct moves are: satisfy it, `pause` the
unit, or `escalate` with a written reason. Editing `harness/core/`, the
manifest, or `.active` to get unstuck is never correct.
