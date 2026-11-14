#!/usr/bin/env python3
"""selftest: the kernel's own self-test, the one suite a stamped project carries (M2, B35).

It finishes in under three seconds and needs only the standard library and PyYAML. It builds a
scratch project in a temporary directory and exercises the configuration loader (schema 2), the
deny-policy scanner, the glob dialect (B34), the check runner and the gating self-lint against it.
It never reads or writes the project it runs in.

Stamped: ``python3 harness/core/tests/selftest.py``. Template: ``python3 core/tests/selftest.py``.
Exit 0 prints ``selftest: OK``; a failing step is named and the exit code is 1.
"""
from __future__ import annotations

import pathlib
import shlex
import sys
import tempfile
import time

PKG_PARENT = pathlib.Path(__file__).resolve().parents[2]  # the directory that holds `core/`
if str(PKG_PARENT) not in sys.path:
    sys.path.insert(0, str(PKG_PARENT))

from core.hosts import claude_code  # noqa: E402
from core.kernel import checks, config, globs, policy, selflint  # noqa: E402
from core.kernel.types import CheckSpec, CheckStatus, Severity  # noqa: E402

SCRATCH_ADAPTER = """\
schema: 2
project: scratch
unit_noun: unit
unit_dir: units
checks:
  - {id: ok, command: "true", severity: fatal}
gates:
  - {id: composed, checks: [ok]}
  - {id: verified, checks: [ok], seals_unit: true}
policy:
  deny:
    - {id: acct, regex: '\\d{6}\\s*-?\\s*\\d{4}', scope: [any], severity: fatal, summary: "no account number on disk", enforces: no-acct}
  ask: []
  blind: []
  protected_paths: []
"""


def account_number_like() -> str:
    # Assembled at run time: no account-number-shaped literal is written in this file.
    return "".join(["010190", "-", "1234"])


def _check_command(exit_code: int) -> str:
    return f"{shlex.quote(sys.executable)} -c 'raise SystemExit({exit_code})'"


def run(root: pathlib.Path) -> list[str]:
    """Run every step against the scratch project; return the names of the steps run."""
    adapter = root / config.ADAPTER_REL
    adapter.parent.mkdir(parents=True)
    adapter.write_text(SCRATCH_ADAPTER)
    steps = []

    def step(name):
        steps.append(name)

    # 1. The schema-2 loader accepts a valid adapter and keeps gate order and sealing.
    cfg = config.load(root)
    assert [g.id for g in cfg.gates] == ["composed", "verified"], cfg.gates
    assert cfg.gates[-1].seals_unit and not cfg.gates[0].seals_unit
    assert cfg.gates[1].needs == ("composed",), cfg.gates[1].needs
    step("config: valid adapter loads, gates ordered, last gate seals")

    # 2. A duplicate gate id is refused, and the message names the offending key.
    broken = SCRATCH_ADAPTER.replace("{id: verified,", "{id: composed,")
    adapter.write_text(broken)
    try:
        config.load(root)
    except config.ConfigError as exc:
        assert any(p.startswith("gates[1].id") for p in exc.problems), exc.problems
    else:
        raise AssertionError("duplicate gate id was accepted")
    adapter.write_text(SCRATCH_ADAPTER)
    step("config: duplicate gate id refused and named")

    # 3. The deny policy fires on a account-number-shaped token and stays quiet on clean text.
    policies = policy.compile_policies(config.load(root))
    hits = policy.scan_text(f"record {account_number_like()}\n", policies, "units/x/notes.md")
    assert [h.rule for h in hits] == ["acct"], hits
    assert policy.scan_text("nothing sensitive here\n", policies, "units/x/notes.md") == []
    step("policy: acct deny fires on a token, not on clean text")

    # 4. The glob dialect: `*` does not cross a separator, and the pattern is anchored (B34).
    assert globs.glob_match("units/*/manifest.yaml", "units/a/manifest.yaml")
    assert not globs.glob_match("units/*/manifest.yaml", "units/a/b/manifest.yaml")
    step("globs: one dialect, * stays inside one segment (B34)")

    # 5. The check runner maps exit codes to statuses (0 pass, 1 fatal).
    passing = checks.run_check(CheckSpec(id="p", command=_check_command(0),
                                         severity=Severity.FATAL), root)
    failing = checks.run_check(CheckSpec(id="f", command=_check_command(1),
                                         severity=Severity.FATAL), root)
    assert passing.status == CheckStatus.PASS, passing
    assert failing.status == CheckStatus.FATAL, failing
    step("checks: exit 0 passes, exit 1 is fatal")

    # 6. The gating self-lint sees a project with no hook registration, and says so.
    messages = claude_code.check_registration(root)
    assert messages == [".claude/settings.json missing (hooks unregistered)"], messages
    gating = [f for f in selflint.run_lint(root, tier="gating")
              if "settings.json" in f.message]
    assert gating, "selflint gating tier did not report the missing hook registration"
    step("selflint: gating tier reports missing hook registration")
    return steps


def main() -> int:
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="gategraph-selftest-") as tmp:
            steps = run(pathlib.Path(tmp))
    except Exception as exc:  # the failing step is the last one printed by the traceback
        print(f"selftest: FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    elapsed = time.monotonic() - started
    print(f"selftest: OK ({len(steps)} steps in {elapsed:.2f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
