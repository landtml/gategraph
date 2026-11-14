"""Tests for core/kernel/checks.py: the check runner (K5).

Covers the exit-code contract (section 1.9, bugs B17 and B42), the process
group kill, the shared hook budget, JSON findings, shell quoting of the
placeholders, the producer digest (section 1.8) and write-trigger selection.

Standard library only. Runs with the system python3 and with .venv/bin/python.
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import hashlib
import json
import os
import shlex
import tempfile
import time
import unittest

from core.kernel import checks
from core.kernel.types import CheckResult, CheckSpec, CheckStatus, Severity

FIXTURES = pathlib.Path(__file__).resolve().parent / "check_fixtures"
PY = sys.executable


def fixture(name: str) -> str:
    return shlex.quote(str(FIXTURES / name))


def spec(command: str, *, id: str = "demo", severity: Severity = Severity.FATAL,
         timeout_s: float | None = None, on: tuple = ()) -> CheckSpec:
    return CheckSpec(id=id, command=command, severity=severity,
                     timeout_s=timeout_s, on=tuple(on))


def run_fixture(name: str, root: str, *, severity: Severity = Severity.FATAL,
                timeout_s: float | None = 30.0, deadline: float | None = None,
                extra: str = "") -> CheckResult:
    command = f"{shlex.quote(PY)} {fixture(name)}"
    if extra:
        command = f"{command} {extra}"
    return checks.run_check(spec(command, severity=severity, timeout_s=timeout_s),
                            root, deadline=deadline)


def _alive(pid: int) -> bool:
    """True while the pid names a live process (a zombie counts as dead)."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            state = handle.read().rsplit(")", 1)[1].split()[0]
    except OSError:
        return False
    return state != "Z"


class ExitCodeContract(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_exit_0_is_pass(self):
        result = run_fixture("exit0.py", self.root)
        self.assertEqual(result.status, CheckStatus.PASS)
        self.assertEqual(result.exit, 0)
        self.assertEqual(result.findings, ())

    def test_exit_1_is_fatal(self):
        result = run_fixture("exit1.py", self.root)
        self.assertEqual(result.status, CheckStatus.FATAL)
        self.assertEqual(result.exit, 1)

    def test_exit_1_with_severity_warn_is_warn(self):
        result = run_fixture("exit1.py", self.root, severity=Severity.WARN)
        self.assertEqual(result.status, CheckStatus.WARN)
        self.assertEqual(result.exit, 1)

    def test_exit_2_is_warn(self):
        result = run_fixture("exit2.py", self.root)
        self.assertEqual(result.status, CheckStatus.WARN)
        self.assertEqual(result.exit, 2)

    def test_B42_exit_codes_follow_the_check_contract(self):
        # lint.py inverted these: 1 advisory, 2 fatal. The check contract is
        # 0 pass, 1 fatal, 2 warning, and the runner must use it.
        self.assertEqual(run_fixture("exit0.py", self.root).status, CheckStatus.PASS)
        self.assertEqual(run_fixture("exit1.py", self.root).status, CheckStatus.FATAL)
        self.assertEqual(run_fixture("exit2.py", self.root).status, CheckStatus.WARN)

    def test_exit_3_is_did_not_run(self):
        result = run_fixture("exit3.py", self.root)
        self.assertEqual(result.status, CheckStatus.DID_NOT_RUN)
        self.assertEqual(result.exit, 3)

    def test_exit_126_is_did_not_run(self):
        result = checks.run_check(spec("sh -c 'exit 126'"), self.root)
        self.assertEqual(result.status, CheckStatus.DID_NOT_RUN)
        self.assertEqual(result.exit, 126)

    def test_exit_127_is_did_not_run(self):
        result = checks.run_check(spec("no-such-checker-gategraph-k5"), self.root)
        self.assertEqual(result.status, CheckStatus.DID_NOT_RUN)
        self.assertEqual(result.exit, 127)

    def test_signal_is_did_not_run(self):
        result = run_fixture("self_kill.py", self.root)
        self.assertEqual(result.status, CheckStatus.DID_NOT_RUN)
        self.assertNotIn(result.exit, (0, 1, 2))

    def test_did_not_run_stays_did_not_run_with_severity_warn(self):
        result = run_fixture("exit3.py", self.root, severity=Severity.WARN)
        self.assertEqual(result.status, CheckStatus.DID_NOT_RUN)


class Timeouts(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_B17_timeout_kills_the_process_group(self):
        pidfile = os.path.join(self.root, "child.pid")
        result = run_fixture("fork_child.py", self.root, timeout_s=1.5,
                             extra=shlex.quote(pidfile))
        self.assertEqual(result.status, CheckStatus.FATAL)
        self.assertIsNone(result.exit)
        self.assertLess(result.duration_s, 20)
        with open(pidfile, encoding="utf-8") as handle:
            child = int(handle.read())
        deadline = time.monotonic() + 5
        while _alive(child) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(_alive(child), "forked child survived the timeout")

    def test_B17_budget_shortened_timeout_is_skipped(self):
        # Hook mode: two seconds of shared budget, a check that wants 60.
        result = run_fixture("sleep_long.py", self.root, timeout_s=60,
                             deadline=time.monotonic() + 2.0)
        self.assertEqual(result.status, CheckStatus.SKIPPED)
        self.assertNotEqual(result.status, CheckStatus.FATAL)
        self.assertLess(result.duration_s, 20)
        self.assertTrue(any("make check" in f.message for f in result.findings))

    def test_B17_budget_exhausted_before_start_is_skipped(self):
        result = run_fixture("exit0.py", self.root, deadline=time.monotonic() + 0.1)
        self.assertEqual(result.status, CheckStatus.SKIPPED)
        self.assertIsNone(result.exit)

    def test_own_timeout_is_fatal_without_budget(self):
        result = run_fixture("sleep_long.py", self.root, timeout_s=1.0)
        self.assertEqual(result.status, CheckStatus.FATAL)
        self.assertIsNone(result.exit)

    def test_own_timeout_is_fatal_when_budget_is_larger(self):
        result = run_fixture("sleep_long.py", self.root, timeout_s=1.0,
                             deadline=time.monotonic() + 60)
        self.assertEqual(result.status, CheckStatus.FATAL)


class Findings(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_json_findings_on_stdout_are_parsed(self):
        result = run_fixture("findings_json.py", self.root)
        self.assertEqual(result.status, CheckStatus.FATAL)
        self.assertEqual(len(result.findings), 2)
        first, second = result.findings
        self.assertEqual(first.rule, "demo.one")
        self.assertEqual(first.severity, Severity.FATAL)
        self.assertEqual(first.path, "a.md")
        self.assertEqual(first.line, 3)
        self.assertEqual(first.message, "first finding")
        self.assertEqual(second.severity, Severity.WARN)
        self.assertIsNone(second.path)
        self.assertIsNone(second.line)

    def test_json_finding_remedy_is_passed_through(self):
        code = ("import json, sys; print(json.dumps({'rule': 'demo.fix', 'severity': 'fatal', "
                "'path': 'a.md', 'line': 1, 'message': 'bad', 'remedy': 'do the thing'})); "
                "sys.exit(1)")
        command = f"{shlex.quote(PY)} -c {shlex.quote(code)}"
        result = checks.run_check(spec(command), self.root)
        self.assertEqual(result.status, CheckStatus.FATAL)
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.findings[0].remedy, "do the thing")

    def test_malformed_json_falls_back_to_three_stderr_lines(self):
        result = run_fixture("findings_malformed.py", self.root)
        self.assertEqual(result.status, CheckStatus.FATAL)
        messages = [f.message for f in result.findings]
        self.assertEqual(messages, ["stderr line 1", "stderr line 2", "stderr line 3"])


class Placeholders(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_placeholders_are_shell_quoted(self):
        out = os.path.join(self.root, "argv.json")
        command = (f"{shlex.quote(PY)} {fixture('argv_record.py')} {shlex.quote(out)}"
                   " {files} {unit} {pack}")
        files = ["a b.txt", "x$(touch HACKED).py"]
        result = checks.run_check(spec(command), self.root, files=files,
                                  unit="u nit", pack="pa'ck")
        self.assertEqual(result.status, CheckStatus.PASS)
        with open(out, encoding="utf-8") as handle:
            recorded = json.load(handle)
        self.assertEqual(recorded, ["a b.txt", "x$(touch HACKED).py", "u nit", "pa'ck"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "HACKED")))


class Digest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.script = os.path.join(self.root, "tool.py")
        with open(self.script, "w", encoding="utf-8") as handle:
            handle.write("import sys\nsys.exit(0)\n")

    def tearDown(self):
        self._tmp.cleanup()

    def _digest(self, command: str) -> str:
        return checks.run_check(spec(command), self.root).digest

    def test_digest_is_sha256_hex_and_stable(self):
        first = self._digest(f"{shlex.quote(PY)} tool.py")
        second = self._digest(f"{shlex.quote(PY)} tool.py")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        int(first, 16)

    def test_digest_changes_when_the_script_bytes_change(self):
        before = self._digest(f"{shlex.quote(PY)} tool.py")
        with open(self.script, "a", encoding="utf-8") as handle:
            handle.write("# edited\n")
        after = self._digest(f"{shlex.quote(PY)} tool.py")
        self.assertNotEqual(before, after)

    def test_digest_covers_the_command_string(self):
        plain = self._digest(f"{shlex.quote(PY)} tool.py")
        flagged = self._digest(f"{shlex.quote(PY)} -S tool.py")
        self.assertNotEqual(plain, flagged)

    def test_digest_is_command_plus_script_bytes(self):
        command = f"{shlex.quote(PY)} tool.py"
        expected = hashlib.sha256()
        expected.update(command.encode("utf-8"))
        expected.update(b"\0tool.py\0")
        with open(self.script, "rb") as handle:
            expected.update(handle.read())
        self.assertEqual(self._digest(command), expected.hexdigest())


class Selection(unittest.TestCase):
    def setUp(self):
        self.gate_only = spec("true", id="gate")
        self.docs = spec("true", id="docs", on=("**/*.md",))
        self.core = spec("true", id="core", on=("core/**/*.py",))
        self.all = (self.gate_only, self.docs, self.core)

    def test_gate_mode_selects_every_check(self):
        self.assertEqual(checks.select_checks(self.all), self.all)

    def test_write_mode_selects_only_matching_on_globs(self):
        self.assertEqual(checks.select_checks(self.all, ["docs/a.md"]), (self.docs,))
        self.assertEqual(checks.select_checks(self.all, ["core/x/y.py"]), (self.core,))
        self.assertEqual(checks.select_checks(self.all, ["core/y.py"]), (self.core,))

    def test_B41_on_glob_uses_kernel_dialect(self):
        # One glob dialect: '*' never crosses '/', '**/' matches zero or more dirs.
        shallow = spec("true", id="shallow", on=("*.md",))
        deep = spec("true", id="deep", on=("**/*.md",))
        pool = (shallow, deep)
        self.assertEqual(checks.select_checks(pool, ["docs/a.md"]), (deep,))
        self.assertEqual(checks.select_checks(pool, ["a.md"]), (shallow, deep))

    def test_gate_only_checks_are_not_write_triggered(self):
        self.assertEqual(checks.select_checks(self.all, ["unrelated.txt"]), ())


if __name__ == "__main__":
    unittest.main(verbosity=2)
