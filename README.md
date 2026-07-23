# Gatebench

[![CI](https://github.com/landtml/gatebench/actions/workflows/ci.yml/badge.svg)](https://github.com/landtml/gatebench/actions/workflows/ci.yml)
[![nightly](https://github.com/landtml/gatebench/actions/workflows/nightly.yml/badge.svg)](https://github.com/landtml/gatebench/actions/workflows/nightly.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![python: 3 stdlib + PyYAML](https://img.shields.io/badge/python-stdlib%20%2B%20PyYAML-3776ab.svg)

A gate harness for coding agents. Gatebench wraps a project in deterministic
gates, evidence-chain manifests, and independent verifiers, so a fixed model
ships work that passes its checks and records the passing as evidence.

The design bet is that reliability can come from outside the model: hold the
model constant and add enforcement around it.

## How it works

**Gates.** A project declares a linear chain of gates. Each gate advances only
when the tool that produced its evidence sets it, so no step is skippable by
assertion. `make check` is the only definition of done. The reference policy
pack stamped in `eval/` runs a five-gate chain.

**Evidence-chain manifests.** A gate records a sha256 of every file it
examined. Edit one of those files afterward and the gate reopens; add a file
the gate never saw and it reopens the same way. A gate earned over zero files
is refused. The manifest is the receipt, and it is checked, not trusted.

**Write-time policy.** A PostToolUse hook runs the project's policy checks on
every write. A banned pattern (an account number on disk, a disclosed client name
in a rendered document) fails at the moment it is written, well before review.
The exit-code contract is strict: `0` pass, `1` fatal, `2` warn, and any other
exit counts as fatal "the check did not run", so a gate is never earned on a
check that crashed.

**Independent verifiers.** A blinded reviewer subagent scores an artifact
against a rubric with no access to the author's justification, then writes a
schema-valid, sha256-anchored review into the unit. The reviewer that tariffs
the work never hears the case for it.

**Core and adapter.** `core/` is stamped verbatim into every project as
`harness/core/` and stays in sync through `gatebench update` (hash-tracked,
never hand-edited). `harness/adapter/` holds the project's own enforcement and
`update` never touches it. The harness grows with the project: every observed
failure becomes a check, and a check general enough to reuse gets promoted back
to the template with `gatebench upstream`.

## Quick start

```sh
git clone https://github.com/landtml/gatebench
cd gatebench
make test-core                                   # fast inner loop, 385 tests in ~17s

python3 gatebench.py init ../my-project          # questionnaire, then stamp
python3 gatebench.py init ../my-project --defaults   # accept every default (CI + agents)
python3 gatebench.py init ../my-project --retrofit   # existing or dirty repo, never overwrites
```

A stamped project gets:

- `harness/core/`, the gatebench-owned skeleton, hash-tracked and synced by `gatebench update`
- `harness/adapter/`, your enforcement: `adapter.yaml`, executable checks, an oracle, a review rubric
- session hooks wired into `.claude/settings.json`
- `make check` as the single gate on done, and a `PROGRESS.yaml` whose items pass only on a green check

## Layout

- `gatebench.py`, the CLI: `init`, `update`, `diff`, `upstream`, `gen`, `add-check`, `set-gate-command`
- `core/`, the harness skeleton stamped into `<project>/harness/core/`, plus `core/eval/` for the measurement machinery
- `seed/`, adapter starters, stamped once at init and never updated after
- `eval/`, the reference corpus (44 synthetic cases) and the policy pack it runs against
- `tests/`, the template's own suite; `core/tests/`, the core suite
- `docs/`, [DESIGN.md](docs/DESIGN.md), [ADAPTER_GUIDE.md](docs/ADAPTER_GUIDE.md), [EVAL.md](docs/EVAL.md), [THREAT-MODEL.md](docs/THREAT-MODEL.md)

## Testing and evaluation

```sh
make test-core   # fast inner loop, 385 tests in ~17s
make test        # test-core plus the template suite, which stamps real projects end to end (~8 min)
make eval        # deterministic corpus, free, a regression net
make chaos       # fault-injection suite (mock legs), run alone
make fuzz        # boundary fuzz suite, run alone
```

`make eval` is expected to pass at 100%: the corpus is authored green against
the current policy pack, and its task is to catch regressions.

## Platforms

Linux and macOS (POSIX). The core relies on POSIX semantics: `os.replace`
atomicity, `fcntl` advisory locks, and signal-based process control. On Windows
the doctor refuses and WSL is the supported path. macOS is expected to work and
awaits one full suite run as its evidence, since evidence over opinion applies
to platform claims too.

## Dependencies

Python 3 standard library and PyYAML. The core takes no other dependency and
imports no Claude Code plugin or skill, so it stays self-contained by design.

## Status

`0.4.0-dev`. The core suite and the deterministic eval are green. Interfaces can still move before a
1.0 tag. Changes are logged in [CHANGELOG.md](CHANGELOG.md).

## License

MIT, © 2026 landtml. See [LICENSE](LICENSE).
