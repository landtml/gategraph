"""S2: proceed, restart and kill behaviour of the variant-search graph (bugs B19, B21).

Offline only: the toy oracle and FakeModel. The checkpointer is the real SQLite one from
core.graph.store, so a second graph built on the same root really is a restart. The kill case
runs this file again with ``--child`` and sends the child SIGKILL. Run standalone with
``.venv/bin/python core/tests/search/test_search_proceed.py``.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from core.graph import runner, store
from core.graph.search import build_search_graph, initial_state, thread_id
from core.kernel.types import Budget

# The fakes live in test_search_graph.py; import them from the sibling file.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from test_search_graph import Clock, Keyed, Oracle, PROJECT  # noqa: E402

REPO = _bootstrap.REPO


def sqlite_graph(root, state_dir, **cfg):
    base = {"project": PROJECT, "state_dir": state_dir, "oracle": cfg.pop("oracle", Oracle()),
            "model": cfg.pop("model", Keyed()), "budget": cfg.pop("budget", Budget(max_iterations=3)),
            "checkpointer": store.make_checkpointer({"root": root}),
            "retry_initial_interval": 0.01}
    base.update(cfg)
    return build_search_graph(base)


def drive(graph, root, *, start=False):
    """Run the thread through the runner. ``start=True`` gives the first input; otherwise the
    runner continues from the last checkpoint (input None), which is a proceed."""
    seen = []
    inp = initial_state(PROJECT) if start else None
    runner.run(graph, thread_id(PROJECT), inp, seen.append,
               lock_dir=Path(root) / "locks")
    return seen[-1]


class TestProceed(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.state_dir = self.root / "state"

    def tearDown(self):
        self._tmp.cleanup()

    def test_B19_wall_clock_survives_restart(self):
        # Run 1 spends 60 s on c000, then the process dies 10 s into c001.
        # The restart is 60 s later (t=130). The 100 s wall-clock budget was measured
        # from the first start, so the restarted run halts at once, without a new c002.
        clock = Clock(0.0)
        crashed = {"done": False}

        def first_clock_run(cid):
            clock.now += 60 if cid == "c000" else 10
            if cid == "c001" and not crashed["done"]:
                crashed["done"] = True
                raise RuntimeError("process killed in c001")

        oracle = Oracle(on_run=first_clock_run)
        graph = sqlite_graph(self.root, self.state_dir, oracle=oracle,
                             budget=Budget(max_wall_s=100), clock=clock)
        with self.assertRaises(RuntimeError):
            drive(graph, self.root, start=True)

        restart_clock = Clock(130.0)
        restart_oracle = Oracle()
        graph = sqlite_graph(self.root, self.state_dir, oracle=restart_oracle,
                             budget=Budget(max_wall_s=100), clock=restart_clock)
        snap = drive(graph, self.root)  # a proceed
        self.assertEqual(snap.values["status"], "abandoned")
        self.assertIn("wall-clock", snap.values["stop_reason"])
        self.assertEqual(restart_oracle.runs, ["c001"])          # c000 is not run again
        self.assertEqual(snap.values["spend"]["iterations"], 2)  # no c002 after the restart

    def test_B21_spend_of_completed_branches_kept_when_one_fails(self):
        # Three variants in one fan-out. c001's oracle dies once. The two that completed
        # keep their spend and their results; proceed must not re-run their steps.
        crashed = {"done": False}

        def boom_once(cid):
            if cid == "c001" and not crashed["done"]:
                crashed["done"] = True
                raise RuntimeError("oracle blew up on c001")

        model = Keyed()
        oracle = Oracle(on_run=boom_once)
        model.price = 0.25  # usd per model call, so the spend can be checked exactly
        graph = sqlite_graph(self.root, self.state_dir, oracle=oracle, model=model,
                             slots=3, budget=Budget(max_iterations=3))
        with self.assertRaises(RuntimeError):
            drive(graph, self.root, start=True)

        graph = sqlite_graph(self.root, self.state_dir, oracle=oracle, model=model, slots=3,
                             budget=Budget(max_iterations=3))
        snap = drive(graph, self.root)  # a proceed
        for cid in ("c000", "c001", "c002"):
            self.assertEqual(model.count(cid, "proposal"), 1, cid)  # never re-proposed
            self.assertEqual(model.count(cid, "judgement"), 1, cid)
        self.assertEqual(oracle.runs.count("c000"), 1)
        self.assertEqual(oracle.runs.count("c002"), 1)
        self.assertEqual(oracle.runs.count("c001"), 2)
        self.assertEqual(snap.values["spend"]["model_calls"], 6)
        self.assertAlmostEqual(snap.values["spend"]["usd"], 1.5)
        self.assertEqual(snap.values["spend"]["iterations"], 3)
        self.assertIn("iteration", snap.values["stop_reason"])


CHILD_MARKERS = "oracle"


class SlowJudge(Keyed):
    """Child-only model: announces each judge call, then stalls so the parent can kill it."""

    def __init__(self, marker, stall_s):
        super().__init__()
        self.marker, self.stall_s = marker, stall_s

    def complete(self, prompt, schema=None):
        if schema["name"] == "judgement":
            with open(self.marker, "a", encoding="utf-8") as fh:
                fh.write("judge\n")
            time.sleep(self.stall_s)
        return super().complete(prompt, schema)


def marked_oracle(marker):
    def on_run(cid):
        with open(marker, "a", encoding="utf-8") as fh:
            fh.write(f"oracle {cid}\n")
    return Oracle(on_run=on_run)


class TestKillMidRun(unittest.TestCase):
    def test_killed_run_proceeds_and_does_not_rerun_oracle_for_a_variant_with_a_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, marker = Path(tmp), Path(tmp) / "markers.txt"
            child = subprocess.Popen([sys.executable, __file__, "--child", str(root), str(marker)])
            try:
                deadline = time.time() + 120
                while time.time() < deadline:
                    if marker.exists() and "judge" in marker.read_text().split("\n"):
                        break
                    time.sleep(0.02)
                else:
                    self.fail("the child never reached a judge call")
                os.kill(child.pid, signal.SIGKILL)
            finally:
                child.wait(timeout=30)
            self.assertEqual(child.returncode, -signal.SIGKILL)

            graph = sqlite_graph(root, root / "state", oracle=marked_oracle(marker),
                                 model=Keyed(), slots=1, budget=Budget(max_iterations=3))
            snap = drive(graph, root)  # a proceed of the killed thread
            self.assertEqual(snap.values["status"], "abandoned")
            self.assertEqual(snap.values["spend"]["iterations"], 3)
            runs = [line.split()[1] for line in marker.read_text().splitlines()
                    if line.startswith("oracle ")]
            self.assertEqual(sorted(runs), ["c000", "c001", "c002"])  # each oracle ran once


def child_main(root, marker):
    graph = sqlite_graph(Path(root), Path(root) / "state", oracle=marked_oracle(marker),
                         model=SlowJudge(marker, stall_s=30), slots=1,
                         budget=Budget(max_iterations=3))
    drive(graph, root, start=True)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--child":
        child_main(sys.argv[2], sys.argv[3])
    else:
        unittest.main(verbosity=2)
