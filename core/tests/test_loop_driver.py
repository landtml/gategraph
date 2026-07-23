"""Layer 2 driver + CLI tests, the P4 exit criteria.

- toy loop runs 5 mock-worker iterations hitting GRADUATE / REJECT / tripwire
  (plus dedup short-circuit and the smoke→repair loop);
- kill -9 mid-CHECK proceeds at CHECK (a real SIGKILL on a subprocess);
- backstops halt the run; refuse-if-unbounded refuses at the CLI;
- escalation driver-backstop (fail streak, self-consistency disagreement);
- reflexion critique→revise bounce (off by default, bounded);
- the loop CLI: run/once/status/validate/verify-flags with --mock-workers.
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fixtures import default_adapter, mkroot, run_cli  # noqa: E402
from core.loop.driver import VARIANT_FILE, Driver  # noqa: E402
from core.loop.events import EventLog  # noqa: E402
from core.loop.gateway import (  # noqa: E402
    CheckResult, MockGateway, ProposeResult, default_mock_check,
)
from core.loop.registry import Registry  # noqa: E402
from core.loop.schemas import ProposalCard, Verdict  # noqa: E402
from core.loop.statefile import Phase, StateStore  # noqa: E402
from core.loop.toy_oracle import ToyOracle  # noqa: E402

PKG_PARENT = Path(__file__).resolve().parents[2]


def scripted_propose(script):
    """propose_fn keyed by variant_id; a list value scripts repair attempts.

    Each entry is (variant_py_text, cell). Captures every call's kwargs in
    .calls for assertions on probation carry / repair feedback.
    """
    counts, calls = {}, []

    def fn(*, variant_id, variant_dir, brief, probation_target=None,
           probation_code=None, repair_feedback="", **_):
        calls.append({"variant_id": variant_id, "brief": brief,
                      "probation_target": probation_target,
                      "probation_code": probation_code,
                      "repair_feedback": repair_feedback})
        d = Path(variant_dir)
        d.mkdir(parents=True, exist_ok=True)
        entry = script[variant_id]
        if isinstance(entry, list):
            i = min(counts.get(variant_id, 0), len(entry) - 1)
            text, cell = entry[i]
        else:
            text, cell = entry
        counts[variant_id] = counts.get(variant_id, 0) + 1
        path = d / VARIANT_FILE
        path.write_text(text)
        card = ProposalCard(id=variant_id, hypothesis="h",
                            mechanism=f"mechanism of {cell}", cell=cell,
                            kill_criteria="k")
        return ProposeResult(card, str(path), cost_usd=0.01, tokens=100)

    fn.calls = calls
    return fn


def scripted_check(script):
    """check_fn keyed by variant_id (a list scripts successive calls)."""
    counts, calls = {}, []

    def judgement(**over):
        base = {"soft_passes": {"mechanism_lead": True, "robustness": True},
                "gaming_clean": True, "mechanism_sentence": "clear mechanism",
                "structurally_promising": True, "exhausted": False,
                "notes": "scripted"}
        base.update(over)
        return base

    def fn(*, variant_id, experiment_path, gate_result, **_):
        calls.append(variant_id)
        entry = script.get(variant_id, {})
        if isinstance(entry, list):
            i = min(counts.get(variant_id, 0), len(entry) - 1)
            over = entry[i]
        else:
            over = entry
        counts[variant_id] = counts.get(variant_id, 0) + 1
        j = judgement(variant_id=variant_id, **over)
        return CheckResult(j, cost_usd=0.01, tokens=80)

    fn.calls = calls
    fn.judgement = judgement
    return fn


REJECT_JUDGEMENT = {"soft_passes": {"mechanism_lead": False, "robustness": False},
                    "structurally_promising": False, "exhausted": True,
                    "mechanism_sentence": ""}


class DriverBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state_root = str(Path(self._tmp.name) / "loopstate")

    def tearDown(self):
        self._tmp.cleanup()

    def mkdriver(self, *, propose_fn=None, check_fn=None, cfg=None,
                 repo_root=None, oracle=None):
        base_cfg = {"backstops": {"max_iterations": 50,
                                  "drift": {"max_attempts_per_variant": 3,
                                            "max_minutes_per_variant": 20}}}
        if cfg:
            base_cfg.update(cfg)
        gw = MockGateway(propose_fn=propose_fn, check_fn=check_fn)
        self.registry = Registry(Path(self.state_root) / "registry.jsonl")
        self.store = StateStore(self.state_root)
        self.log = EventLog(Path(self.state_root) / "events.jsonl")
        return Driver(oracle=oracle or ToyOracle(), gateway=gw,
                      registry=self.registry, store=self.store, log=self.log,
                      cfg=base_cfg, state_root=self.state_root,
                      repo_root=repo_root, sleeper=lambda s: None)


class TestFiveIterationToyLoop(DriverBase):
    """The headline P4 exit test: 5 mock iterations, every major branch."""

    def test_graduate_tripwire_reject_dedup_and_repair(self):
        propose = scripted_propose({
            "c000": ("OK = True\n", "cell-zero"),
            "c001": ("TOY_TRIPWIRES = {'leak': True}\n", "cell-one"),
            "c002": ("OK = True\n", "cell-two"),
            "c003": ("OK = True\n", "cell-two"),   # dedup: cell-two is dead by now
            "c004": [("def broken(:\n", "cell-four"),      # smoke fail → repair
                     ("OK = True\n", "cell-four")],
        })
        check = scripted_check({"c002": REJECT_JUDGEMENT})
        drv = self.mkdriver(propose_fn=propose, check_fn=check)
        completed = drv.run(max_iterations=5)
        self.assertEqual(completed, 5)

        # verdict landscape
        self.assertEqual(self.registry.graduated_ids(), ["c000", "c004"])
        self.assertEqual(self.registry.dead_end_cells(), {"cell-two"})

        # tripwire REJECT skipped CHECK and did not burn its cell
        self.assertEqual(check.calls.count("c001"), 0)
        verdicts = {e["variant_id"]: e for e in self.registry.events()
                    if e.get("kind") == "verdict"}
        self.assertFalse(verdicts["c001"]["voids_cell"])
        self.assertIn("tripwire", verdicts["c001"]["why"])

        # dedup duplicate: zero oracle compute, no proposal card recorded
        cand3 = Path(self.state_root) / "variants" / "c003"
        self.assertFalse((cand3 / "gate_result.json").exists())
        dd = json.loads((cand3 / "dedup.json").read_text())
        self.assertEqual(dd["source"], "exact")
        proposals = {e["id"] for e in self.registry.events()
                     if e.get("kind") == "proposal"}
        self.assertEqual(proposals, {"c000", "c001", "c002", "c004"})

        # smoke→repair: maker got the traceback, repair breadcrumb was cleared
        repair_calls = [c for c in propose.calls
                        if c["variant_id"] == "c004" and c["repair_feedback"]]
        self.assertEqual(len(repair_calls), 1)
        self.assertIn("SyntaxError", repair_calls[0]["repair_feedback"])
        self.assertFalse(
            (Path(self.state_root) / "variants" / "c004" / "_repair.txt").exists())

        # graduation sketch artifact
        self.assertTrue((Path(self.state_root) / "graduated" / "c000"
                         / "SKETCH.md").exists())

        # every iteration file ends DONE (crash-safe bookkeeping)
        for i in range(5):
            self.assertEqual(self.store.load(i).phase, Phase.DONE.value)


class TestProceedAfterKill9(DriverBase):
    """kill -9 mid-CHECK leaves phase==check on disk; a fresh driver re-enters
    exactly there. A real SIGKILL on a subprocess, no in-process simulation."""

    RUNNER = """
import pathlib, sys, time
sys.path.insert(0, sys.argv[2])
from core.loop.driver import Driver
from core.loop.events import EventLog
from core.loop.gateway import MockGateway
from core.loop.registry import Registry
from core.loop.statefile import StateStore
from core.loop.toy_oracle import ToyOracle

state_root = sys.argv[1]

def slow_check(**kw):
    pathlib.Path(state_root, "CHECK_STARTED").touch()
    time.sleep(120)
    raise SystemExit("never reached")

drv = Driver(oracle=ToyOracle(), gateway=MockGateway(check_fn=slow_check),
             registry=Registry(pathlib.Path(state_root) / "registry.jsonl"),
             store=StateStore(state_root),
             log=EventLog(pathlib.Path(state_root) / "events.jsonl"),
             cfg={"backstops": {"max_iterations": 10}}, state_root=state_root)
drv.run(max_iterations=1)
"""

    def test_sigkill_mid_check_proceeds_at_check(self):
        runner = Path(self._tmp.name) / "runner.py"
        runner.write_text(self.RUNNER)
        proc = subprocess.Popen(
            [sys.executable, str(runner), self.state_root, str(PKG_PARENT)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        sentinel = Path(self.state_root) / "CHECK_STARTED"
        try:
            deadline = time.time() + 30
            while not sentinel.exists():
                self.assertIsNone(proc.poll(), "runner died before CHECK")
                self.assertLess(time.time(), deadline, "CHECK never started")
                time.sleep(0.05)
            os.kill(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()

        # the crash left the phase-to-run persisted as CHECK
        store = StateStore(self.state_root)
        state = store.load_latest()
        self.assertEqual(state.phase, Phase.CHECK.value)

        # a fresh driver (fast gateway) proceeds at CHECK and completes
        drv = self.mkdriver()
        completed = drv.run(max_iterations=1)
        self.assertEqual(completed, 1)
        self.assertEqual(self.registry.graduated_ids(), ["c000"])
        proceeded = [e for e in self.log.events() if e["from"] == "check"]
        self.assertTrue(proceeded, "no check→… receipt after proceed")
        self.assertEqual(store.load_latest().phase, Phase.DONE.value)


class TestBackstopsHaltTheRun(DriverBase):
    def test_wall_clock_zero_halts_before_first_iteration(self):
        drv = self.mkdriver(cfg={"backstops": {"wall_clock_minutes": 0}})
        self.assertEqual(drv.run(max_iterations=3), 0)
        self.assertIn("wall-clock", drv._banner)

    def test_iteration_cap_halts_second_start(self):
        drv = self.mkdriver(cfg={"backstops": {"max_iterations": 1}})
        self.assertEqual(drv.run(max_iterations=5), 1)
        self.assertIn("iteration cap", drv._banner)

    def test_cost_budget_halts(self):
        drv = self.mkdriver(cfg={"backstops": {"cost_budget_usd": 0.005}})
        self.assertEqual(drv.run(max_iterations=5), 1)
        self.assertIn("cost budget", drv._banner)

    def test_pause_file_yields(self):
        Path(self.state_root).mkdir(parents=True, exist_ok=True)
        (Path(self.state_root) / "PAUSE").touch()
        drv = self.mkdriver()
        self.assertEqual(drv.run(max_iterations=3), 0)
        self.assertIn("PAUSED", drv._banner)


class TestProtectedFileMidRunHalt(DriverBase):
    def test_adapter_edit_mid_run_halts_with_banner(self):
        repo = Path(self._tmp.name) / "repo"
        (repo / "harness" / "adapter").mkdir(parents=True)
        (repo / "harness" / "core" / "loop").mkdir(parents=True)
        adapter = repo / "harness" / "adapter" / "adapter.yaml"
        adapter.write_text("fh_schema: 1\n")

        def editing_check(**kw):
            adapter.write_text("fh_schema: 1  # drifted mid-run\n")
            return default_mock_check(**kw)

        drv = self.mkdriver(check_fn=editing_check, repo_root=repo)
        completed = drv.run(max_iterations=3)
        self.assertEqual(completed, 1)
        self.assertIn("protected", drv._banner)


class TestEscalationDriverBackstop(DriverBase):
    def test_fail_streak_escalates_instead_of_looping(self):
        propose = scripted_propose({
            "c000": ("OK = True\n", "cell-a"),
            "c001": ("OK = True\n", "cell-b"),
            "c002": ("OK = True\n", "cell-c"),
        })
        check = scripted_check({"c000": REJECT_JUDGEMENT,
                                "c001": REJECT_JUDGEMENT,
                                "c002": REJECT_JUDGEMENT})
        drv = self.mkdriver(propose_fn=propose, check_fn=check,
                            cfg={"escalation": {"fail_streak_to_escalate": 2}})
        completed = drv.run(max_iterations=5)
        self.assertEqual(completed, 2, "third iteration must not start")
        esc = list((Path(self.state_root) / "escalations").glob("ESCALATION_*.md"))
        self.assertTrue(esc, "no escalation receipt written")
        self.assertIn("fail streak", esc[0].read_text().lower())
        self.assertIn("ESCALAT", drv._banner.upper())

    def test_self_consistency_disagreement_recorded_and_below_tau_proceeds(self):
        check = scripted_check({"c000": [
            {}, {}, {"structurally_promising": False,
                     "soft_passes": {"mechanism_lead": False, "robustness": True}},
        ]})
        drv = self.mkdriver(check_fn=check, cfg={
            "self_consistency": {"enabled": True, "n": 3},
            "escalation": {"error_prob_threshold": None}})
        self.assertEqual(drv.run(max_iterations=1), 1)
        self.assertEqual(len(check.calls), 3, "self-consistency must sample n=3")
        j = json.loads((Path(self.state_root) / "variants" / "c000"
                        / "checker_judgement.json").read_text())
        sc = j["self_consistency"]
        self.assertEqual(sc["n"], 3)
        self.assertGreater(sc["disagreement"], 0.0)
        # majority still graduates
        self.assertEqual(self.registry.graduated_ids(), ["c000"])

    def test_disagreement_above_tau_escalates_and_stops(self):
        check = scripted_check({"c000": [
            {}, {"structurally_promising": False}, {"exhausted": True},
        ]})
        drv = self.mkdriver(check_fn=check, cfg={
            "self_consistency": {"enabled": True, "n": 3},
            "escalation": {"error_prob_threshold": 0.01}})
        drv.run(max_iterations=3)
        esc = list((Path(self.state_root) / "escalations").glob("ESCALATION_*.md"))
        self.assertTrue(esc, "disagreement above τ must write an escalation")
        self.assertIn("disagreement", esc[0].read_text().lower())
        # aborted mid-iteration: CHECK is still the phase to run on proceed
        self.assertEqual(StateStore(self.state_root).load_latest().phase,
                         Phase.CHECK.value)
        self.assertEqual(self.registry.graduated_ids(), [])


class TestReflexion(DriverBase):
    def test_reject_bounces_to_revise_once_then_records(self):
        below_bar = {"soft_passes": {"mechanism_lead": False, "robustness": False},
                     "structurally_promising": False, "exhausted": False,
                     "mechanism_sentence": "", "notes": "weak evidence"}
        propose = scripted_propose({"c000": ("OK = True\n", "cell-a")})
        check = scripted_check({"c000": [below_bar, {}]})
        drv = self.mkdriver(propose_fn=propose, check_fn=check,
                            cfg={"reflexion": {"enabled": True, "max_iters": 1}})
        self.assertEqual(drv.run(max_iterations=1), 1)
        self.assertEqual(self.registry.graduated_ids(), ["c000"])
        # the revise pass handed the maker the critique
        revise_calls = [c for c in propose.calls if c["repair_feedback"]]
        self.assertEqual(len(revise_calls), 1)
        self.assertIn("weak evidence", revise_calls[0]["repair_feedback"])
        self.assertTrue(any("reflexion" in e.get("detail", "")
                            for e in self.log.events()))

    def test_reflexion_off_by_default_records_reject_directly(self):
        below_bar = {"soft_passes": {"mechanism_lead": False, "robustness": False},
                     "structurally_promising": False, "exhausted": False,
                     "mechanism_sentence": ""}
        propose = scripted_propose({"c000": ("OK = True\n", "cell-a")})
        check = scripted_check({"c000": below_bar})
        drv = self.mkdriver(propose_fn=propose, check_fn=check)
        drv.run(max_iterations=1)
        self.assertEqual(len(check.calls), 1)
        verdicts = [e for e in self.registry.events() if e.get("kind") == "verdict"]
        self.assertEqual(verdicts[0]["verdict"], Verdict.REJECT.value)


class TestProbationCarry(DriverBase):
    def test_probation_target_and_code_reach_the_next_maker(self):
        propose = scripted_propose({
            "c000": ("OK = True  # near-miss original\n", "cell-a"),
            "c001": ("OK = True\n", "cell-a2"),
        })
        near_miss = {"soft_passes": {"mechanism_lead": False, "robustness": True}}
        check = scripted_check({"c000": near_miss})
        drv = self.mkdriver(propose_fn=propose, check_fn=check)
        self.assertEqual(drv.run(max_iterations=2), 2)
        carry = propose.calls[1]
        self.assertEqual(carry["probation_target"], "mechanism_lead")
        self.assertIn("near-miss original", carry["probation_code"] or "")


class TestMetricFloorsInTheLoop(DriverBase):
    def test_floor_failure_turns_into_a_probation_target(self):
        propose = scripted_propose({
            "c000": ("TOY_METRICS = {'score': 0.1}\n", "cell-a")})
        drv = self.mkdriver(propose_fn=propose,
                            cfg={"gates": {"metric_floors": {"score": 0.8}}})
        drv.run(max_iterations=1)
        verdicts = [e for e in self.registry.events() if e.get("kind") == "verdict"]
        self.assertEqual(verdicts[0]["verdict"], Verdict.PROBATION.value)
        self.assertEqual(verdicts[0]["target_pass"], "score")


class TestLoopCli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        adapter = default_adapter()
        adapter["loop"] = dict(adapter["loop"],
                               enabled=True,
                               oracle="core.loop.toy_oracle:ToyOracle")
        self.root = mkroot(Path(self._tmp.name), adapter)

    def tearDown(self):
        self._tmp.cleanup()

    def cli(self, *args):
        return run_cli("loop/cli.py", *args, root=self.root)

    def adapter_dict(self):
        import yaml
        return yaml.safe_load(
            (self.root / "harness" / "adapter" / "adapter.yaml").read_text())

    def write_adapter(self, adapter):
        from fixtures import write_adapter
        write_adapter(self.root, adapter)

    def test_run_mock_workers_one_iteration(self):
        r = self.cli("run", "--mock-workers", "--max-iterations", "1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        state = self.root / "harness" / "state" / "loop"
        self.assertTrue((state / "registry.jsonl").exists())
        self.assertTrue((state / "STATUS.md").exists())

    def test_status_after_run(self):
        self.cli("run", "--mock-workers", "--max-iterations", "1")
        r = self.cli("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Graduated", r.stdout)

    def test_run_refuses_when_disabled(self):
        adapter = self.adapter_dict()
        adapter["loop"]["enabled"] = False
        self.write_adapter(adapter)
        r = self.cli("run", "--mock-workers")
        self.assertEqual(r.returncode, 2)
        self.assertIn("enabled", (r.stdout + r.stderr))

    def test_run_refuses_unbounded(self):
        adapter = self.adapter_dict()
        adapter["loop"]["backstops"] = {"max_iterations": None,
                                        "cost_budget_usd": None,
                                        "wall_clock_minutes": None}
        self.write_adapter(adapter)
        r = self.cli("run", "--mock-workers")
        self.assertEqual(r.returncode, 2)
        self.assertIn("backstop", (r.stdout + r.stderr))

    def test_validate_subcommand(self):
        r = self.cli("validate")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        adapter = self.adapter_dict()
        adapter["loop"]["backstops"] = {"max_iterations": None,
                                        "cost_budget_usd": None,
                                        "wall_clock_minutes": None}
        self.write_adapter(adapter)
        r = self.cli("validate")
        self.assertEqual(r.returncode, 2)
        # disabled loop never gates make check
        adapter["loop"]["enabled"] = False
        self.write_adapter(adapter)
        r = self.cli("validate")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_verify_flags_structural(self):
        r = self.cli("verify-flags")
        self.assertIn(r.returncode, (0, 2), r.stderr)
        self.assertIn("write-scope guard", r.stdout)
        self.assertIn("FLAGS.md", r.stdout)

    @unittest.skipUnless(os.environ.get("FH_LIVE_SMOKE") == "1",
                         "live haiku smoke: set FH_LIVE_SMOKE=1 to spend")
    def test_verify_flags_live_haiku_probe(self):
        r = self.cli("verify-flags", "--live")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("live", r.stdout.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
