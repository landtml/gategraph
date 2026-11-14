"""S2: the variant-search graph (plan section 1.14; bugs B19-B22).

Offline only: the toy oracle and FakeModel, never a real model. Run standalone with
``.venv/bin/python core/tests/search/test_search_graph.py``.

The fakes are FakeModel instances, one per model call, chosen by the variant id and the
schema name that the prompt carries (``Variant id: cNNN``). Proceed and kill cases are in
test_search_proceed.py.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import dataclasses
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from core.agent.models import FakeModel, Structured
from core.graph import runner
from core.graph.search import (aggregate_judgements, build_search_graph, initial_state,
                               thread_id)
from core.kernel.registry import Registry
from core.kernel.toy_oracle import ToyOracle
from core.kernel.types import Budget, Spend

ROOT = _bootstrap.REPO
CID = re.compile(r"Variant id: (\S+)")
PROJECT = "demo"
CLEAN_JUDGE = {"soft_passes": {"mechanism_lead": True, "robustness": True},
               "gaming_clean": True, "mechanism_sentence": "a clear mechanism",
               "structurally_promising": True, "exhausted": False, "notes": "clean"}
REJECT_JUDGE = {"soft_passes": {"mechanism_lead": False, "robustness": False},
                "gaming_clean": True, "mechanism_sentence": "",
                "structurally_promising": False, "exhausted": True, "notes": "below bar"}


class RateLimited(Exception):
    """Named like the legacy worker error: core.kernel.faults matches on the class name."""
    retry_after_s = 0.0


class Keyed:
    """Model fake: each call is answered by a one-step FakeModel chosen by variant and kind.

    ``proposals`` and ``judges`` map a variant id to a list of answers consumed in order;
    when the list is empty the clean default answer is used. ``raise_on`` maps
    (variant, kind) to exceptions raised before the answer. ``price`` is usd per call.
    """

    def __init__(self, proposals=None, judges=None, *, raise_on=None, price=0.0):
        self.proposals = {k: list(v) for k, v in (proposals or {}).items()}
        self.judges = {k: list(v) for k, v in (judges or {}).items()}
        self.raise_on = {k: list(v) for k, v in (raise_on or {}).items()}
        self.price = price
        self.calls = {}

    def _default_proposal(self, cid):
        return {"cell": f"cell-{cid}", "hypothesis": "h", "mechanism": "m",
                "kill_criteria": "k", "source": "OK = True\n"}

    def complete(self, prompt, schema=None):
        cid = CID.search(prompt).group(1)
        kind = schema["name"]
        key = (cid, kind)
        self.calls[key] = self.calls.get(key, 0) + 1
        if self.raise_on.get(key):
            raise self.raise_on[key].pop(0)
        if kind == "proposal":
            queue, default = self.proposals.get(cid, []), self._default_proposal(cid)
        else:
            queue, default = self.judges.get(cid, []), CLEAN_JUDGE
        answer = queue.pop(0) if queue else default
        res = FakeModel([Structured(answer)]).complete(prompt, schema)
        if self.price:
            res = dataclasses.replace(res, spend=Spend(usd=self.price, model_calls=1))
        return res

    def count(self, cid, kind):
        return self.calls.get((cid, kind), 0)


class Oracle(ToyOracle):
    """The toy oracle, recording which variants it ran. ``on_run`` may raise or advance a clock."""

    def __init__(self, on_run=None):
        self.runs = []
        self.on_run = on_run

    def run_gates(self, variant_path, cfg):
        cid = Path(variant_path).parent.name
        self.runs.append(cid)
        if self.on_run:
            self.on_run(cid)
        return super().run_gates(variant_path, cfg)

    def proposal_space(self):
        return []  # any cell is allowed; the toy's three cells would run out


class Clock:
    def __init__(self, now=0.0):
        self.now = float(now)

    def __call__(self):
        return self.now


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.state_dir = self.tmp / "state"
        self.lock_dir = self.tmp / "locks"

    def tearDown(self):
        self._tmp.cleanup()

    def build(self, model, oracle=None, *, budget=None, **extra):
        cfg = {"project": PROJECT, "state_dir": self.state_dir,
               "oracle": oracle or Oracle(), "model": model,
               "budget": budget or Budget(max_iterations=3),
               "checkpointer": InMemorySaver(), "retry_initial_interval": 0.01}
        cfg.update(extra)
        return build_search_graph(cfg)

    def run_graph(self, graph, input_or_command=None):
        """Drive the graph through the runner, as the CLI does. Returns the last snapshot."""
        seen = []
        inp = initial_state(PROJECT) if input_or_command is None else input_or_command
        runner.run(graph, thread_id(PROJECT), inp, seen.append, lock_dir=self.lock_dir)
        return seen[-1]

    def registry(self):
        return Registry(self.state_dir / "registry.jsonl")

    def proposals(self, cid):
        return [e for e in self.registry().events() if e.get("kind") == "proposal" and e["id"] == cid]

    def verdict(self, cid):
        latest = [e for e in self.registry().events()
                  if e.get("kind") == "verdict" and e["variant_id"] == cid]
        return latest[-1]["verdict"] if latest else None


class TestCleanRun(Case):
    def test_clean_run_graduates(self):
        oracle = Oracle()
        graph = self.build(Keyed(), oracle, budget=Budget(max_iterations=5),
                           graduate_target=1)
        snap = self.run_graph(graph)
        self.assertEqual(snap.values["status"], "complete")
        self.assertIn("target", snap.values["stop_reason"])
        self.assertEqual(self.registry().graduated_ids(), ["c000"])
        self.assertEqual(oracle.runs, ["c000"])


class TestSmokeRepair(Case):
    def test_smoke_bug_repaired_and_proposal_recorded_only_after_smoke(self):
        model = Keyed(proposals={"c000": [
            {"cell": "cell-a", "hypothesis": "h", "mechanism": "m", "kill_criteria": "k",
             "source": "def broken(:\n"},
            {"cell": "cell-a", "hypothesis": "h", "mechanism": "fixed", "kill_criteria": "k",
             "source": "OK = True\n"}]})
        oracle = Oracle()
        graph = self.build(model, oracle, max_repairs=1, graduate_target=1)
        snap = self.run_graph(graph)
        self.assertEqual(model.count("c000", "proposal"), 2)      # one repair
        self.assertEqual(oracle.runs, ["c000"])                   # oracle saw only the fixed file
        proposals = self.proposals("c000")
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0]["mechanism"], "fixed")       # recorded after smoke passed
        self.assertEqual(snap.values["repairs"], 1)
        self.assertEqual(self.verdict("c000"), "GRADUATE")


class TestDuplicate(Case):
    def test_exact_duplicate_rejected_without_calling_oracle(self):
        model = Keyed(proposals={"c000": [{"cell": "Cell-X", "hypothesis": "h",
                                           "mechanism": "m", "kill_criteria": "k",
                                           "source": "OK = True\n"}]},
                      judges={"c000": [REJECT_JUDGE]})
        oracle = Oracle()
        model.proposals["c001"] = [{"cell": " cell-x ", "hypothesis": "h", "mechanism": "m",
                                    "kill_criteria": "k", "source": "OK = True\n"}]
        graph = self.build(model, oracle, budget=Budget(max_iterations=2))
        snap = self.run_graph(graph)
        self.assertEqual(self.verdict("c001"), "REJECT")
        self.assertEqual(oracle.runs, ["c000"])                   # c001 never reached the oracle
        self.assertEqual(model.count("c001", "judgement"), 0)
        self.assertEqual(snap.values["status"], "abandoned")


class TestRateLimit(Case):
    def test_rate_limit_retries_and_never_counts_against_variant(self):
        model = Keyed(raise_on={("c000", "proposal"): [RateLimited("slow down")]})
        graph = self.build(model, budget=Budget(max_iterations=1), graduate_target=1)
        snap = self.run_graph(graph)
        self.assertEqual(model.count("c000", "proposal"), 2)      # failed once, retried
        self.assertEqual(self.verdict("c000"), "GRADUATE")
        self.assertEqual(snap.values["fail_streak"], 0)
        self.assertEqual(snap.values["repairs"], 0)
        self.assertEqual(snap.values["reproposals"], 0)


class TestBackstops(Case):
    def halts(self, budget, *, model=None, oracle=None, expect, **extra):
        graph = self.build(model or Keyed(), oracle or Oracle(), budget=budget, **extra)
        snap = self.run_graph(graph)
        self.assertEqual(snap.values["status"], "abandoned", snap.values)
        self.assertIn(expect, snap.values["stop_reason"])

    def test_iteration_backstop_halts_with_named_reason(self):
        self.halts(Budget(max_iterations=1), expect="iteration")

    def test_cost_backstop_halts_with_named_reason(self):
        self.halts(Budget(max_usd=0.4), model=Keyed(price=0.25), expect="cost")

    def test_model_call_backstop_halts_with_named_reason(self):
        self.halts(Budget(max_model_calls=1, max_iterations=5), expect="model-call")

    def test_wall_clock_backstop_halts_with_named_reason(self):
        clock = Clock()
        oracle = Oracle(on_run=lambda cid: setattr(clock, "now", clock.now + 60))
        self.halts(Budget(max_wall_s=50), oracle=oracle, expect="wall-clock", clock=clock)

    def test_graduate_target_stops_the_run_as_complete(self):
        graph = self.build(Keyed(), budget=Budget(max_iterations=5), graduate_target=2)
        snap = self.run_graph(graph)
        self.assertEqual(snap.values["status"], "complete")
        self.assertEqual(len(self.registry().graduated_ids()), 2)


class TestEscalation(Case):
    def test_B20_escalation_proceeds_on_retry_and_resets_streak(self):
        model = Keyed(judges={"c000": [REJECT_JUDGE], "c001": [REJECT_JUDGE],
                              "c002": [REJECT_JUDGE]})
        graph = self.build(model, budget=Budget(max_iterations=4), graduate_target=1,
                           fail_streak_to_escalate=2)
        snap = self.run_graph(graph)
        self.assertEqual(snap.values["status"], "in_progress")
        pending = runner.pending(graph, thread_id(PROJECT))
        self.assertEqual(pending[0]["kind"], "escalation")
        self.assertEqual(pending[0]["fail_streak"], 2)
        snap = self.run_graph(graph, Command(resume="retry"))
        # c002 is rejected too. Without the reset its streak would be 3 and the run would stop
        # again at once; with it, the streak is 1 and c003 graduates.
        self.assertEqual(runner.pending(graph, thread_id(PROJECT)), [])
        self.assertEqual(len(snap.values["decisions"]), 1)
        self.assertEqual(snap.values["decisions"][0]["answer"], "retry")
        self.assertEqual(snap.values["decisions"][0]["fail_streak"], 2)
        self.assertEqual(self.verdict("c002"), "REJECT")
        self.assertEqual(self.verdict("c003"), "GRADUATE")
        self.assertEqual(snap.values["status"], "complete")

    def test_stop_answer_ends_the_run_as_escalated(self):
        model = Keyed(judges={"c000": [REJECT_JUDGE], "c001": [REJECT_JUDGE]})
        graph = self.build(model, budget=Budget(max_iterations=5), fail_streak_to_escalate=2)
        self.run_graph(graph)
        snap = self.run_graph(graph, Command(resume="stop"))
        self.assertEqual(snap.values["status"], "escalated")
        self.assertIn("escalation", snap.values["stop_reason"])


class TestCounters(Case):
    def test_B22_three_counters_are_independent(self):
        # One smoke repair uses the whole repair budget. The revision budget is untouched by
        # it, so the critique-revise bounce still happens.
        model = Keyed(proposals={"c000": [
            {"cell": "cell-r", "hypothesis": "h", "mechanism": "m", "kill_criteria": "k",
             "source": "def broken(:\n"},
            {"cell": "cell-r", "hypothesis": "h", "mechanism": "", "kill_criteria": "k",
             "source": "OK = True\n"},
            {"cell": "cell-r", "hypothesis": "h", "mechanism": "revised", "kill_criteria": "k",
             "source": "OK = True\n"}]},
            judges={"c000": [REJECT_JUDGE]})
        graph = self.build(model, max_repairs=1, max_revisions=1, max_reproposals=0,
                           graduate_target=1)
        snap = self.run_graph(graph)
        self.assertEqual(model.count("c000", "proposal"), 3)
        self.assertEqual(snap.values["repairs"], 1)
        self.assertEqual(snap.values["revisions"], 1)
        self.assertEqual(snap.values["reproposals"], 0)
        self.assertEqual(self.verdict("c000"), "GRADUATE")


class TestToyOracle(Case):
    def test_toy_oracle_clean_run_graduates_as_legacy_e2e(self):
        from core.kernel.toy_oracle import ToyOracle as KernelToy
        # The kernel toy's proposal space is its three named cells, so the proposal uses one.
        model = Keyed(proposals={"c000": [{"cell": "toy-alpha", "hypothesis": "h",
                                           "mechanism": "m", "kill_criteria": "k",
                                           "source": "OK = True\n"}]})
        graph = self.build(model, KernelToy(), budget=Budget(max_iterations=2),
                           graduate_target=1)
        self.run_graph(graph)
        self.assertEqual(self.registry().graduated_ids(), ["c000"])

    def test_declared_soft_passes_give_the_legacy_probation_target(self):
        # The legacy toy scored mechanism_lead and robustness as soft passes (4 in total).
        # A kernel toy that declares them must reach the same PROBATION, with the same target.
        class SoftToy(ToyOracle):
            def soft_passes(self):
                return ["mechanism_lead", "robustness"]

            def proposal_space(self):
                return []

        near_miss = dict(CLEAN_JUDGE, soft_passes={"mechanism_lead": False, "robustness": True},
                         structurally_promising=True)
        model = Keyed(judges={"c000": [near_miss]})
        graph = self.build(model, SoftToy(), budget=Budget(max_iterations=1))
        self.run_graph(graph)
        probation = [e for e in self.registry().events()
                     if e.get("kind") == "verdict" and e["variant_id"] == "c000"]
        self.assertEqual(probation[-1]["verdict"], "PROBATION")
        self.assertEqual(probation[-1]["target_pass"], "mechanism_lead")


class TestAggregate(unittest.TestCase):
    def test_aggregate_judgements_majority_and_disagreement(self):
        yes = {"soft_passes": {"a": True}, "gaming_clean": True, "mechanism_sentence": "m",
               "structurally_promising": True, "exhausted": False}
        no = {"soft_passes": {"a": False}, "gaming_clean": True, "mechanism_sentence": "m",
              "structurally_promising": False, "exhausted": False}
        agg, disagreement = aggregate_judgements([yes, yes, no])
        self.assertEqual(agg["soft_passes"], {"a": True})
        self.assertTrue(agg["structurally_promising"])
        # five voted fields: soft pass a (1/3), gaming (0), promising (1/3), exhausted (0),
        # mechanism sentence (0)
        self.assertAlmostEqual(disagreement, (2 / 3) / 5)
        unanimous, zero = aggregate_judgements([yes, yes])
        self.assertEqual(zero, 0.0)
        with self.assertRaises(ValueError):
            aggregate_judgements([])


class TestCli(unittest.TestCase):
    def test_cli_mock_run_status_validate(self):
        with tempfile.TemporaryDirectory() as root:
            cli = str(ROOT / "core" / "cli" / "search.py")
            py = str(ROOT / ".venv" / "bin" / "python")
            common = ["--root", root, "--project", PROJECT]
            run = subprocess.run([py, cli, "run", *common, "--mock", "--max-iterations", "2",
                                  "--oracle", "core.kernel.toy_oracle:ToyOracle"],
                                 capture_output=True, text=True, timeout=120)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            status = subprocess.run([py, cli, "status", *common], capture_output=True,
                                    text=True, timeout=120)
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertIn("status: abandoned", status.stdout)
            self.assertIn("iteration", status.stdout)
            check = subprocess.run([py, cli, "validate", "--root", root, "--project", PROJECT,
                                    "--oracle", "core.kernel.toy_oracle:ToyOracle",
                                    "--max-iterations", "2"],
                                   capture_output=True, text=True, timeout=120)
            self.assertEqual(check.returncode, 0, check.stderr)
            refused = subprocess.run([py, cli, "validate", "--root", root, "--project", PROJECT,
                                      "--oracle", "core.kernel.toy_oracle:ToyOracle"],
                                     capture_output=True, text=True, timeout=120)
            self.assertNotEqual(refused.returncode, 0)  # no bound: refused


class TestLegacyParity(unittest.TestCase):
    """The new --mock toy run must give the same verdicts and STATUS.md as legacy --mock-workers."""

    def run_mock(self, root, iterations=10):
        cli = str(ROOT / "core" / "cli" / "search.py")
        py = str(ROOT / ".venv" / "bin" / "python")
        return subprocess.run([py, cli, "run", "--root", root, "--project", PROJECT, "--mock",
                               "--max-iterations", str(iterations),
                               "--oracle", "core.kernel.toy_oracle:ToyOracle"],
                              capture_output=True, text=True, timeout=300)

    def test_mock_toy_run_graduates_like_legacy(self):
        with tempfile.TemporaryDirectory() as root:
            run = self.run_mock(root, 10)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            registry = Path(root) / ".gategraph" / "search" / PROJECT / "registry.jsonl"
            events = Registry(registry).events()
            proposals = [e for e in events if e.get("kind") == "proposal"]
            verdicts = [e for e in events if e.get("kind") == "verdict"]
            self.assertEqual(len(proposals), 10, "the iteration backstop bounds the run at 10")
            self.assertEqual(len(verdicts), 10, "every iteration reaches a verdict")
            self.assertEqual({v["verdict"] for v in verdicts}, {"GRADUATE"},
                             "the toy oracle declares its soft passes; the mock judges them clean")
            self.assertEqual(len(Registry(registry).graduated_ids()), 10)

    def test_status_md_written(self):
        with tempfile.TemporaryDirectory() as root:
            run = self.run_mock(root, 10)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            state = Path(root) / ".gategraph" / "search" / PROJECT
            self.assertTrue((state / "registry.jsonl").is_file())
            status = state / "STATUS.md"
            self.assertTrue(status.is_file(), "STATUS.md must sit next to registry.jsonl")
            text = status.read_text(encoding="utf-8")
            for needle in ("# search: run status", "**Graduated: 10/?**", "iterations: 10",
                           "- Graduated: c000, c001", "- Probation (re-queue): none",
                           "- Dead-end cells: none", "**Stop:**", "## Current iteration",
                           "## Recent transitions", "- c009 [GRADUATE]"):
                self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
