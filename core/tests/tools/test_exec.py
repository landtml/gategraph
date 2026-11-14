"""Tests for core/tools/exec.py: the run_command tool (plan section 1.10, brief A2).

Covers: the executable allow-list, the working directory confined to the unit,
the wall-clock timeout that kills the whole process group, `touched` as the
exact set of changed files under the declared writable paths, output truncation
with a marker, the environment filter (names ending in _KEY, _TOKEN, _SECRET
are dropped unless the policy lists them), and the no-shell argument model.

Runs with .venv/bin/python (the tool layer). Runnable on its own:
    .venv/bin/python core/tests/tools/test_exec.py
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import json
import os
import tempfile
import time
import unittest

from core.kernel.types import Action, ActionKind, Budget
from core.tools.spec import ToolContext, ToolResult, ToolSpec
from core.tools.exec import (
    CommandRefused,
    ExecPolicy,
    changed_paths,
    command_action,
    make_run_command_tool,
    run_command,
    snapshot,
)

PY = sys.executable
HAS_PROC = os.path.isdir("/proc/self")


class _Journal:
    """In-memory effects journal; run_command does not use it, the context needs one."""

    def __init__(self):
        self.records = {}

    def seen(self, key):
        return self.records.get(key)

    def record(self, key, result):
        self.records[key] = result


def _policy(**overrides) -> ExecPolicy:
    kwargs = {"allowed": {"py": PY}}
    kwargs.update(overrides)
    return ExecPolicy(**kwargs)


def _context(root: pathlib.Path) -> ToolContext:
    unit = root / "unit"
    scratch = root / "scratch"
    (unit / "out").mkdir(parents=True)
    (unit / "sub").mkdir()
    scratch.mkdir()
    return ToolContext(
        root=root,
        unit_dir=unit,
        scratch_dir=scratch,
        writable=("unit/out",),
        blind=(),
        budget=Budget(),
        journal=_Journal(),
    )


def _alive(pid: int) -> bool:
    """True when the process exists and is not a zombie (a killed process may linger as Z)."""
    try:
        with open(f"/proc/{pid}/stat") as handle:
            state = handle.read().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return False
    return state != "Z"


class TestAllowList(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ctx = _context(pathlib.Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_unlisted_executable_is_refused(self):
        policy = _policy()
        for argv in (["sh", "-c", "echo hi"], ["/bin/sh", "-c", "echo hi"]):
            with self.subTest(argv=argv):
                with self.assertRaises(CommandRefused):
                    run_command({"argv": argv}, self.ctx, policy)
                with self.assertRaises(CommandRefused):
                    command_action({"argv": argv}, policy)

    def test_path_spelling_of_a_listed_name_is_refused(self):
        # Only the exact key is accepted, never a path that happens to reach the same file.
        policy = _policy()
        for argv in ([PY, "-c", "print(1)"], ["./py", "-c", "print(1)"], ["bin/py", "-c", "1"]):
            with self.subTest(argv=argv):
                with self.assertRaises(CommandRefused):
                    run_command({"argv": argv}, self.ctx, policy)

    def test_listed_name_runs_the_mapped_executable(self):
        result = run_command({"argv": ["py", "-c", "print('ran')"]}, self.ctx, _policy())
        self.assertIn("ran", result.content)
        self.assertIn("exit 0", result.content)

    def test_policy_rejects_bad_configuration(self):
        bad = [
            {"allowed": {"a/b": PY}},          # key must be a bare name
            {"allowed": {"py": "python"}},     # value must be an absolute path
            {"max_output_chars": 50},          # too small to hold the marker
            {"default_timeout_s": 0},
            {"default_timeout_s": 1000.0, "max_timeout_s": 600.0},
        ]
        for overrides in bad:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    _policy(**overrides)


class TestWorkingDirectory(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.ctx = _context(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_cwd_outside_the_unit_is_refused(self):
        outside = self.root / "elsewhere"
        outside.mkdir()
        os.symlink(outside, self.ctx.unit_dir / "escape")
        policy = _policy()
        for cwd in ("..", "../scratch", str(outside), "escape"):
            with self.subTest(cwd=cwd):
                with self.assertRaises(CommandRefused):
                    run_command({"argv": ["py", "-c", "1"], "cwd": cwd}, self.ctx, policy)

    def test_cwd_inside_the_unit_is_used(self):
        result = run_command(
            {"argv": ["py", "-c", "import os; print(os.getcwd())"], "cwd": "sub"},
            self.ctx, _policy())
        printed = result.content.split("\n", 1)[1].strip()
        self.assertEqual(
            pathlib.Path(printed).resolve(),
            (self.ctx.unit_dir / "sub").resolve())

    def test_default_cwd_is_the_unit(self):
        result = run_command({"argv": ["py", "-c", "import os; print(os.getcwd())"]},
                             self.ctx, _policy())
        printed = result.content.split("\n", 1)[1].strip()
        self.assertEqual(pathlib.Path(printed).resolve(), self.ctx.unit_dir.resolve())


@unittest.skipUnless(HAS_PROC, "needs /proc to inspect process state")
class TestTimeout(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ctx = _context(pathlib.Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_timeout_kills_the_whole_process_group(self):
        # The command starts a grandchild, records both pids, then sleeps past the timeout.
        script = (
            "import os, pathlib, subprocess, sys, time\n"
            "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            "pathlib.Path(sys.argv[1]).write_text(f'{os.getpid()} {g.pid}')\n"
            "time.sleep(120)\n"
        )
        pidfile = self.ctx.scratch_dir / "pids.txt"
        started = time.monotonic()
        result = run_command(
            {"argv": ["py", "-c", script, str(pidfile)], "timeout_s": 2.0},
            self.ctx, _policy())
        elapsed = time.monotonic() - started

        self.assertIn("timed out", result.content)
        self.assertLess(elapsed, 30.0)
        self.assertTrue(pidfile.exists(), "the child never recorded its pids")
        parent_pid, grandchild_pid = (int(p) for p in pidfile.read_text().split())
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and (_alive(parent_pid) or _alive(grandchild_pid)):
            time.sleep(0.05)
        self.assertFalse(_alive(parent_pid), "timed-out command is still running")
        self.assertFalse(_alive(grandchild_pid), "grandchild survived the group kill")

    def test_timeout_outside_policy_bounds_is_refused(self):
        policy = _policy(default_timeout_s=1.0, max_timeout_s=5.0)
        for timeout in (0, -1, 6.0, True, "1"):
            with self.subTest(timeout=timeout):
                with self.assertRaises(CommandRefused):
                    run_command({"argv": ["py", "-c", "1"], "timeout_s": timeout},
                                self.ctx, policy)


class TestTouched(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.ctx = _context(self.root)
        out = self.root / "unit" / "out"
        (out / "a.txt").write_text("one")
        (out / "b.txt").write_text("two")
        (out / "same.txt").write_text("unchanged")
        (self.root / "unit" / "outside.txt").write_text("not writable")

    def tearDown(self):
        self._tmp.cleanup()

    def test_touched_is_exactly_the_changed_files_under_writable_paths(self):
        script = (
            "import pathlib\n"
            "out = pathlib.Path('out')\n"
            "(out / 'a.txt').write_text('ONE')\n"          # content changed
            "(out / 'b.txt').unlink()\n"                   # removed
            "(out / 'd.txt').write_text('new')\n"          # created
            "(out / 'same.txt').write_text('unchanged')\n" # rewritten, same bytes
            "pathlib.Path('outside.txt').write_text('changed')\n"  # not writable
        )
        result = run_command({"argv": ["py", "-c", script]}, self.ctx, _policy())
        self.assertEqual(result.touched, ("unit/out/a.txt", "unit/out/b.txt", "unit/out/d.txt"))

    def test_no_writes_means_nothing_touched(self):
        result = run_command({"argv": ["py", "-c", "print('read only')"]},
                             self.ctx, _policy())
        self.assertEqual(result.touched, ())

    def test_touched_entries_are_workspace_relative_results(self):
        result = run_command({"argv": ["py", "-c", "open('out/x.txt','w').write('x')"]},
                             self.ctx, _policy())
        self.assertEqual(result.touched, ("unit/out/x.txt",))
        ToolResult(content="", touched=result.touched)  # the spec validator accepts it

    def test_writable_path_must_stay_inside_the_workspace(self):
        for bad in ("../escape", "/abs/path", "unit/../../x"):
            with self.subTest(writable=bad):
                with self.assertRaises(ValueError):
                    snapshot(self.root, (bad,))

    def test_snapshot_and_diff_helpers(self):
        before = snapshot(self.root, ("unit/out",))
        (self.root / "unit" / "out" / "a.txt").write_text("changed")
        after = snapshot(self.root, ("unit/out",))
        self.assertEqual(changed_paths(before, after), ("unit/out/a.txt",))


class TestOutput(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ctx = _context(pathlib.Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_long_output_is_truncated_to_the_limit_with_a_marker(self):
        limit = 1000
        result = run_command({"argv": ["py", "-c", "print('x' * 50000)"]},
                             self.ctx, _policy(max_output_chars=limit))
        self.assertLessEqual(len(result.content), limit)
        self.assertIn("truncated", result.content)
        self.assertTrue(result.content.endswith("characters]"))

    def test_short_output_has_no_marker(self):
        result = run_command({"argv": ["py", "-c", "print('short')"]},
                             self.ctx, _policy(max_output_chars=1000))
        self.assertNotIn("truncated", result.content)
        self.assertIn("short", result.content)

    def test_exit_code_and_stderr_are_reported(self):
        result = run_command(
            {"argv": ["py", "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"]},
            self.ctx, _policy())
        self.assertIn("exit 3", result.content)
        self.assertIn("boom", result.content)

    def test_arguments_are_literal_no_shell(self):
        hostile = "$HOME; echo pwned > /dev/null"
        result = run_command(
            {"argv": ["py", "-c", "import sys; print(sys.argv[1])", hostile]},
            self.ctx, _policy())
        self.assertIn(hostile, result.content)


class TestEnvironment(unittest.TestCase):

    SECRET_NAMES = ("TEST_EXEC_FAKE_KEY", "TEST_EXEC_FAKE_TOKEN", "TEST_EXEC_FAKE_SECRET",
                    "test_exec_lower_key")
    LISTED = "TEST_EXEC_LISTED_TOKEN"
    KEPT = "TEST_EXEC_KEPT_PLAIN"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ctx = _context(pathlib.Path(self._tmp.name))
        self._saved = dict(os.environ)
        for name in self.SECRET_NAMES:
            os.environ[name] = f"fake-value-{name}"
        os.environ[self.LISTED] = "fake-value-listed"
        os.environ[self.KEPT] = "plain-value"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)
        self._tmp.cleanup()

    def _child_env(self, policy):
        script = "import json, os, sys; json.dump(dict(os.environ), sys.stdout)"
        result = run_command({"argv": ["py", "-c", script]}, self.ctx, policy)
        return json.loads(result.content.split("\n", 1)[1]), result

    def test_secret_suffixed_variables_are_not_passed(self):
        env, result = self._child_env(_policy(max_output_chars=500_000))
        for name in self.SECRET_NAMES:
            self.assertNotIn(name, env)
        self.assertNotIn(self.LISTED, env)
        self.assertEqual(env.get(self.KEPT), "plain-value")
        for name in self.SECRET_NAMES:
            self.assertNotIn(f"fake-value-{name}", result.content)

    def test_listed_secret_variable_is_passed(self):
        env, _ = self._child_env(_policy(max_output_chars=500_000,
                                         env_allow=frozenset({self.LISTED})))
        self.assertEqual(env.get(self.LISTED), "fake-value-listed")
        self.assertNotIn("TEST_EXEC_FAKE_KEY", env)


class TestSpec(unittest.TestCase):

    def test_action_is_exec_and_carries_the_command(self):
        action = command_action({"argv": ["py", "-c", "print(1)"]}, _policy())
        self.assertIsInstance(action, Action)
        self.assertEqual(action.kind, ActionKind.EXEC)
        self.assertEqual(action.tool, "run_command")
        self.assertIn("print", action.command)

    def test_spec_declares_effects_and_is_not_idempotent(self):
        spec = make_run_command_tool(_policy())
        self.assertIsInstance(spec, ToolSpec)
        self.assertEqual(spec.name, "run_command")
        self.assertIn("exec", spec.effects)
        self.assertFalse(spec.idempotent)
        self.assertFalse(spec.needs_approval)
        action = spec.action_for({"argv": ["py", "-c", "1"]})
        self.assertEqual(action.kind, ActionKind.EXEC)
        with self.assertRaises(CommandRefused):
            spec.action_for({"argv": ["sh", "-c", "1"]})

    def test_spec_runs_through_the_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = _context(pathlib.Path(tmp))
            spec = make_run_command_tool(_policy())
            result = spec.run({"argv": ["py", "-c", "print('via spec')"]}, ctx)
            self.assertIn("via spec", result.content)


if __name__ == "__main__":
    unittest.main()
