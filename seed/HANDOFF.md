# HANDOFF

Structured consolidation, not a dump. The stop hook (`harness/core/hosts/shims/stop_gate.py`)
blocks ending a session while a unit was touched after this file was last written.

## Now

Fresh project, no active unit yet.

## Next

Run `make venv` once, then `make check`, then start the first unit:
`.venv/bin/python harness/core/cli/gate.py start --unit <slug>`.

## Decisions this session

(none yet)

## Gotchas

(none yet)
