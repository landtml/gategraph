"""Tests for core/graph/runner.py: the only code that invokes a graph (plan section 1.15).

Runs with .venv/bin/python. The graph is a two-node stub defined here: "step"
(the work node) and "gate" (the check node), looping until a target count. The
cross-process cases run this same file with --child-* arguments.
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import json
import operator
import os
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from typing import Annotated, TypedDict

from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from core.graph import runner, store
from core.kernel import paths
from core.kernel.locks import LockHeld

REPO = _bootstrap.REPO
THREAD = "example/report-01"


class Stub(TypedDict, total=False):
    n: int
    target: int
    trail: Annotated[list, operator.add]


def build(checkpointer, *, target=5, pause_at=None, fail_at=None,
          pause_s=0.0, progress=None, hold_at=None, release=None, ready=None):
    """A two-node loop. Hooks are optional and exist only for the tests."""

    def step(state):
        n = state.get("n", 0) + 1
        if fail_at is not None and n == fail_at:
            raise RuntimeError("stub failure at step %d" % n)
        if pause_s:
            time.sleep(pause_s)
        if progress:
            with open(progress, "a", encoding="utf-8") as handle:
                handle.write("%d\n" % n)
        return {"n": n, "trail": ["step"]}

    def gate(state):
        n = state["n"]
        if pause_at is not None and n == pause_at:
            answer = interrupt({"question": "approve", "n": n})
            return {"trail": ["gate", str(answer)]}
        if hold_at is not None and n == hold_at and ready and release:
            pathlib.Path(ready).write_text("ready", encoding="utf-8")
            while not os.path.exists(release):
                time.sleep(0.02)
        return {"trail": ["gate"]}

    def route(state):
        return END if state["n"] >= state.get("target", target) else "step"

    g = StateGraph(Stub)
    g.add_node("step", step)
    g.add_node("gate", gate)
    g.add_edge(START, "step")
    g.add_edge("step", "gate")
    g.add_conditional_edges("gate", route)
    return g.compile(checkpointer=checkpointer)


def start_state(target):
    return {"n": 0, "target": target}


class RunnerCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(tmp.name)

    def saver(self):
        return store.make_checkpointer({"root": self.root})


def _child_main(argv):
    """Entry for the subprocess cases. Prints one JSON line and exits 0."""
    mode, root, thread = argv[0], argv[1], argv[2]
    rest = argv[3:]
    saver = store.make_checkpointer({"root": root})
    out = {}
    if mode == "--child-hold":
        ready, release = rest
        app = build(saver, target=6, hold_at=2, ready=ready, release=release)
        result = runner.run(app, thread, start_state(6), lambda snap: None)
        out["n"] = result.get("n")
    elif mode == "--child-try":
        app = build(saver, target=6)
        try:
            runner.run(app, thread, start_state(6), lambda snap: None)
            out["refused"] = False
        except LockHeld as exc:
            out = {"refused": True, "message": str(exc), "holder": exc.holder}
    elif mode == "--child-kill":
        progress = rest[0]
        app = build(saver, target=60, pause_s=0.05, progress=progress)
        runner.run(app, thread, start_state(60), lambda snap: None)
        out["finished"] = True
    print(json.dumps(out), flush=True)


class TestRunnerLocking(RunnerCase):
    def test_B49_second_runner_on_same_thread_refused(self):
        ready = self.root / "ready"
        release = self.root / "release"
        script = pathlib.Path(__file__).resolve()
        holder = subprocess.Popen(
            [sys.executable, str(script), "--child-hold", str(self.root), THREAD,
             str(ready), str(release)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.time() + 60
            while not ready.exists() and time.time() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists(), "first runner never reached its hold point")
            tried = subprocess.run(
                [sys.executable, str(script), "--child-try", str(self.root), THREAD],
                capture_output=True, text=True, timeout=60)
            self.assertEqual(tried.returncode, 0, tried.stderr)
            got = json.loads(tried.stdout.strip().splitlines()[-1])
            self.assertTrue(got["refused"], got)
            self.assertIn("pid %d" % holder.pid, got["message"])
            self.assertEqual(got["holder"].get("pid"), holder.pid)
        finally:
            release.write_text("go", encoding="utf-8")
            out, err = holder.communicate(timeout=60)
        self.assertEqual(holder.returncode, 0, err)
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["n"], 6)

    def test_two_units_run_concurrently(self):
        results, spans, errors = {}, {}, {}
        barrier = threading.Barrier(2, timeout=60)

        def unit(name):
            try:
                saver = self.saver()
                app = build(saver, target=30, pause_s=0.01)
                barrier.wait()
                start = time.time()
                snap = []
                out = runner.run(app, "example/%s" % name, start_state(30), snap.append)
                spans[name] = (start, time.time())
                results[name] = (out.get("n"), snap[0].next)
            except BaseException as exc:  # reported by the assertions below
                errors[name] = repr(exc)
                barrier.abort()

        threads = [threading.Thread(target=unit, args=(n,)) for n in ("unit-a", "unit-b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        self.assertEqual(errors, {})
        self.assertEqual(results["unit-a"], (30, ()))
        self.assertEqual(results["unit-b"], (30, ()))
        overlap = max(s for s, _ in spans.values()) < min(e for _, e in spans.values())
        self.assertTrue(overlap, "the two runs did not overlap in time: %r" % spans)

    def test_kill_minus_nine_then_proceed(self):
        progress = self.root / "progress"
        script = pathlib.Path(__file__).resolve()
        child = subprocess.Popen(
            [sys.executable, str(script), "--child-kill", str(self.root), THREAD,
             str(progress)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.time() + 60
        while time.time() < deadline:
            if progress.exists() and len(progress.read_text().split()) >= 5:
                break
            time.sleep(0.02)
        child.send_signal(signal.SIGKILL)
        child.communicate(timeout=60)
        self.assertEqual(child.returncode, -signal.SIGKILL)
        killed_at = int(progress.read_text().split()[-1])
        self.assertLess(killed_at, 60, "the child finished before it could be killed")

        snap = []
        out = runner.run(build(self.saver(), target=60), THREAD, None, snap.append)
        self.assertEqual(out["n"], 60)
        self.assertEqual(snap[0].next, ())


class TestLockLocation(RunnerCase):
    def test_default_lock_dir_is_locks_beside_checkpoint_db_in_state_dir(self):
        app = build(self.saver(), target=3)
        runner.run(app, THREAD, start_state(3), lambda snap: None)
        lock = runner.lock_path(paths.state_dir(self.root) / "locks", THREAD)
        self.assertTrue(lock.is_file(), "the lock file must be in state_dir/locks")
        self.assertEqual(runner._default_lock_dir(app), paths.state_dir(self.root) / "locks")


class TestProjectionHook(RunnerCase):
    def test_projection_hook_called_after_every_return(self):
        seen = []
        out = runner.run(build(self.saver(), target=3), THREAD, start_state(3), seen.append)
        self.assertEqual(out["n"], 3)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].next, ())
        self.assertEqual(seen[0].values["n"], 3)

    def test_projection_hook_called_after_an_interrupt(self):
        seen = []
        runner.run(build(self.saver(), target=3, pause_at=2), THREAD,
                   start_state(3), seen.append)
        self.assertEqual(len(seen), 1)
        interrupts = [i.value for t in seen[0].tasks for i in t.interrupts]
        self.assertEqual(interrupts, [{"question": "approve", "n": 2}])

    def test_projection_hook_called_after_an_exception(self):
        seen = []
        app = build(self.saver(), target=5, fail_at=3)
        with self.assertRaises(RuntimeError):
            runner.run(app, THREAD, start_state(5), seen.append)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].values["n"], 2)

    def test_pending_reports_interrupt_payloads(self):
        app = build(self.saver(), target=3, pause_at=2)
        runner.run(app, THREAD, start_state(3), lambda snap: None)
        self.assertEqual(runner.pending(app, THREAD), [{"question": "approve", "n": 2}])
        self.assertEqual(runner.pending(app, "example/no-such-unit"), [])

    def test_checkpoint_reader_returns_values_or_none(self):
        app = build(self.saver(), target=3)
        reader = runner.checkpoint_reader(app)
        self.assertIsNone(reader("example/never-ran"))
        runner.run(app, THREAD, start_state(3), lambda snap: None)
        got = reader(THREAD)
        self.assertEqual(got["values"]["n"], 3)
        self.assertTrue(got["checkpoint_id"])


class TestRecursionLimit(RunnerCase):
    def test_recursion_limit_is_explicit(self):
        seen = []
        app = build(self.saver(), target=10 ** 6)
        with self.assertRaises(GraphRecursionError):
            runner.run(app, THREAD, start_state(10 ** 6), seen.append, recursion_limit=10)
        self.assertEqual(len(seen), 1)
        self.assertLess(seen[0].values["n"], 10)

    def test_default_limit_is_an_explicit_constant(self):
        self.assertIsInstance(runner.DEFAULT_RECURSION_LIMIT, int)
        self.assertLess(runner.DEFAULT_RECURSION_LIMIT, 10007)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1].startswith("--child-"):
        _child_main(sys.argv[1:])
        sys.exit(0)
    unittest.main(verbosity=2)
