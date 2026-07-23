"""T7 chaos suite (mock legs), fault injection at the loop's phase boundaries.

The contract: every injected fault must end in a restartable state
or a typed escalation, never a spin, never a traceback, never a file needing
hand-surgery. These are the RED detectors for the
pre-live-loop hardening batch (F20/F21/F26/F27), kept as a permanent fixture
(``make chaos``). The live-worker leg (real `claude -p` under fault) is
deliberately absent, it spends and needs the user's budget nod (T7/F5).

Legs:
- F20: a torn final line in events.jsonl / registry.jsonl (kill -9 mid-append)
  must not brick reads OR the next append; interior corruption still raises.
- F21: a persistently-raising oracle / expired auth / budget exhaustion must
  stop after a bounded retry budget with a typed escalation, and aborted
  attempts must not count as completed iterations; `cli.py run` must exit
  non-zero on a fault-shaped stop (backstop halts stay exit 0: bounded runs
  END that way by design).
- F26: contract-strike retries must keep their spend visible to the budget
  backstop (summed onto the final WorkerResult / carried on the exception).
- F27: a timed-out worker's whole process GROUP dies, not just the child.
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fixtures import default_adapter, mkroot, run_cli  # noqa: E402
from test_loop_driver import DriverBase, scripted_propose  # noqa: E402
from test_loop_workers import MockRunner, envelope  # noqa: E402
from core.loop import workers  # noqa: E402
from core.loop.contracts import JUDGE_CONTRACT  # noqa: E402
from core.loop.events import EventLog  # noqa: E402
from core.loop.gateway import MockGateway  # noqa: E402
from core.loop.registry import Registry  # noqa: E402
from core.loop.statefile import Phase  # noqa: E402
from core.loop.toy_oracle import ToyOracle  # noqa: E402
from core.loop.workers import AuthError, BudgetExceeded, ContractError  # noqa: E402


class RaisingOracle(ToyOracle):
    """The ordinary project-oracle bug: run_gates raises persistently."""

    def __init__(self, fail_times=None):
        super().__init__()
        self.calls = 0
        self.fail_times = fail_times  # None → always raise

    def run_gates(self, variant_path, cfg):
        self.calls += 1
        if self.fail_times is None or self.calls <= self.fail_times:
            raise RuntimeError("oracle data file missing (chaos injection)")
        return super().run_gates(variant_path, cfg)


# ── F20: torn JSONL tails ─────────────────────────────────────────────────────

TORN = '{"iteration": 99, "from": "pace", "cost_usd": 0.'  # no closing, no \n


class TestTornEventsLog(DriverBase):
    def _run_one(self):
        drv = self.mkdriver()
        self.assertEqual(drv.run(max_iterations=1), 1)
        return Path(self.state_root) / "events.jsonl"

    def test_torn_final_line_is_tolerated_on_read(self):
        path = self._run_one()
        n_clean = len(EventLog(path).events())
        with path.open("a", encoding="utf-8") as fh:
            fh.write(TORN)
        log = EventLog(path)
        self.assertEqual(len(log.events()), n_clean,
                         "a torn FINAL line must be tolerated, not brick reads")
        self.assertGreater(log.total_cost_usd(), 0.0)

    def test_proceed_after_torn_tail_appends_cleanly(self):
        path = self._run_one()
        with path.open("a", encoding="utf-8") as fh:
            fh.write(TORN)
        # a fresh driver must proceed AND its appends must not concatenate onto
        # the torn fragment (which would corrupt the log's interior forever)
        drv = self.mkdriver()
        self.assertEqual(drv.run(max_iterations=1), 1)
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                json.loads(line)  # every persisted line must parse now

    def test_torn_interior_line_still_raises(self):
        path = self._run_one()
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        lines.insert(1, TORN + "\n")  # interior garbage = real corruption
        path.write_text("".join(lines), encoding="utf-8")
        with self.assertRaises(json.JSONDecodeError):
            EventLog(path).events()

    def test_torn_registry_tail_survives_the_next_record(self):
        self._run_one()
        reg_path = Path(self.state_root) / "registry.jsonl"
        n_clean = len(Registry(reg_path).events())
        with reg_path.open("a", encoding="utf-8") as fh:
            fh.write(TORN)
        drv = self.mkdriver()  # records new proposal+verdict cards
        self.assertEqual(drv.run(max_iterations=1), 1)
        events = Registry(reg_path).events()  # must not raise
        self.assertGreaterEqual(len(events), n_clean + 2,
                                "cards recorded after a torn tail were lost")


# ── F21: persistent infra faults must stop bounded, typed, honest ─────────────

class TestInfraFaultBounded(DriverBase):
    def test_persistently_raising_oracle_stops_bounded(self):
        oracle = RaisingOracle()
        drv = self.mkdriver(oracle=oracle)
        completed = drv.run(max_iterations=50)
        self.assertEqual(completed, 0,
                         "aborted attempts must not count as completed iterations")
        self.assertLessEqual(oracle.calls, 5,
                             f"loop spun {oracle.calls}× on a persistent fault")
        self.assertIn("ESCALAT", drv._banner.upper())
        self.assertTrue(getattr(drv, "faulted", False),
                        "a fault-shaped stop must be typed on the driver")
        esc = list((Path(self.state_root) / "escalations").glob("ESCALATION_*.md"))
        self.assertTrue(esc, "no escalation receipt for the persistent fault")

    def test_expired_auth_at_propose_stops_bounded(self):
        calls = []

        def bad_propose(**kw):
            calls.append(1)
            raise AuthError("credentials expired (chaos injection)")

        drv = self.mkdriver(propose_fn=bad_propose)
        self.assertEqual(drv.run(max_iterations=50), 0)
        self.assertLessEqual(len(calls), 5, f"spun {len(calls)}× on expired auth")
        self.assertIn("ESCALAT", drv._banner.upper())

    def test_budget_exhaustion_mid_check_stops_bounded(self):
        def bad_check(**kw):
            raise BudgetExceeded("worker hit --max-budget-usd (chaos injection)")

        drv = self.mkdriver(check_fn=bad_check)
        self.assertEqual(drv.run(max_iterations=50), 0)
        self.assertIn("ESCALAT", drv._banner.upper())

    def test_transient_fault_recovers_within_the_retry_budget(self):
        oracle = RaisingOracle(fail_times=2)
        drv = self.mkdriver(oracle=oracle)
        self.assertEqual(drv.run(max_iterations=1), 1,
                         "a transient fault within the budget must recover")
        self.assertFalse(getattr(drv, "faulted", False))
        self.assertEqual(self.registry.graduated_ids(), ["c000"])


class TestChaosCliExitCodes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.adapter = default_adapter()
        self.adapter["loop"] = dict(self.adapter["loop"], enabled=True,
                                    oracle="core.loop.toy_oracle:ToyOracle")

    def tearDown(self):
        self._tmp.cleanup()

    def test_run_exits_nonzero_on_escalated_stop(self):
        self.adapter["loop"]["oracle"] = "adapter.chaos_oracle:ChaosOracle"
        root = mkroot(Path(self._tmp.name), self.adapter)
        (root / "harness" / "adapter" / "chaos_oracle.py").write_text(
            "class ChaosOracle:\n"
            "    def incumbent(self):\n"
            "        return None\n"
            "    def run_gates(self, variant_path, cfg):\n"
            "        raise RuntimeError('oracle data file missing (chaos)')\n"
            "    def proposal_space(self):\n"
            "        return ['cell']\n",
            encoding="utf-8")
        r = run_cli("loop/cli.py", "run", "--mock-workers",
                    "--max-iterations", "40", root=root)
        self.assertNotEqual(r.returncode, 0,
                            "an ESCALATED stop must not exit 0:\n" + r.stdout + r.stderr)
        self.assertIn("ESCALAT", (r.stdout + r.stderr).upper())

    def test_backstop_halt_still_exits_zero(self):
        # a bounded run ENDS via its backstop by design, that is not a fault
        self.adapter["loop"]["backstops"] = dict(
            self.adapter["loop"].get("backstops") or {}, max_iterations=1)
        root = mkroot(Path(self._tmp.name), self.adapter)
        r = run_cli("loop/cli.py", "run", "--mock-workers", root=root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


# ── F26: strike spend must stay visible to the budget backstop ────────────────

GOOD = json.dumps({"decision": "DISTINCT", "confidence": 1.0, "reason": "r"})
BAD = json.dumps({"decision": "MAYBE", "confidence": 2, "reason": "r"})


class TestStrikeCostAccounting(unittest.TestCase):
    def test_run_contract_sums_spend_across_strike_attempts(self):
        runner = MockRunner([(0, envelope(BAD, cost=0.40), ""),
                             (0, envelope(BAD, cost=0.40), ""),
                             (0, envelope(GOOD, cost=0.05), "")])
        data, res = workers.run_contract(workers.WorkerSpec(prompt="p"),
                                         JUDGE_CONTRACT, runner, max_strikes=3)
        self.assertEqual(data["decision"], "DISTINCT")
        self.assertAlmostEqual(res.cost_usd, 0.85, places=6,
                               msg="strike attempts' spend vanished from the result")
        self.assertEqual(res.usage.get("input_tokens"), 30)
        self.assertEqual(res.usage.get("output_tokens"), 15)

    def test_contract_error_carries_the_full_spend(self):
        runner = MockRunner([(0, envelope(BAD, cost=0.40), "")] * 3)
        with self.assertRaises(ContractError) as ctx:
            workers.run_contract(workers.WorkerSpec(prompt="p"),
                                 JUDGE_CONTRACT, runner, max_strikes=3)
        self.assertAlmostEqual(getattr(ctx.exception, "cost_usd", 0.0), 1.20,
                               places=6, msg="ContractError lost the strikes' spend")
        self.assertEqual(getattr(ctx.exception, "tokens", 0), 45)

    def test_infra_error_mid_strikes_carries_prior_spend(self):
        runner = MockRunner([
            (0, envelope(BAD, cost=0.40), ""),
            (0, envelope("x", is_error=True, subtype="error_max_budget_usd",
                         cost=0.30), ""),
        ])
        with self.assertRaises(BudgetExceeded) as ctx:
            workers.run_contract(workers.WorkerSpec(prompt="p"),
                                 JUDGE_CONTRACT, runner, max_strikes=3)
        self.assertAlmostEqual(getattr(ctx.exception, "cost_usd", 0.0), 0.70,
                               places=6, msg="infra abort lost the prior strikes' spend")


class TestStrikeSpendReachesReceipts(DriverBase):
    def test_contract_error_spend_lands_on_the_reject_receipt(self):
        exc = ContractError("worker output failed schema after 3 strikes",
                            ["bad"], 3)
        exc.cost_usd, exc.tokens = 0.85, 4500  # what a fixed run_contract attaches

        def striking_propose(**kw):
            raise exc

        drv = self.mkdriver(propose_fn=striking_propose)
        drv.run(max_iterations=1)
        self.assertGreaterEqual(
            self.log.total_cost_usd(), 0.85,
            "strike spend never reached the receipts the budget backstop reads")


# ── F27: worker timeout must kill the process GROUP ───────────────────────────

CHILD_SCRIPT = """
import json, subprocess, sys, time
grand = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
with open(sys.argv[1], "w") as fh:
    json.dump({"grandchild": grand.pid}, fh)
print(json.dumps({"result": "never returned"}))
time.sleep(60)
"""


class TestTimeoutKillsProcessGroup(unittest.TestCase):
    def test_grandchild_dies_with_the_timed_out_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "child.py"
            script.write_text(CHILD_SCRIPT, encoding="utf-8")
            pid_file = Path(tmp) / "grand.json"
            t0 = time.monotonic()
            with self.assertRaises(workers.WorkerTimeout):
                workers.subprocess_runner(
                    [sys.executable, str(script), str(pid_file)],
                    dict(os.environ), 2)
            self.assertLess(time.monotonic() - t0, 30,
                            "timeout did not enforce the wall-clock bound")
            self.assertTrue(pid_file.exists(), "child never started")
            grand_pid = json.loads(pid_file.read_text())["grandchild"]
            alive = True
            try:
                for _ in range(30):  # up to ~3s for SIGKILL delivery + reap
                    try:
                        os.kill(grand_pid, 0)
                    except ProcessLookupError:
                        alive = False
                        break
                    time.sleep(0.1)
                self.assertFalse(
                    alive, f"grandchild {grand_pid} survived the worker timeout")
            finally:
                if alive:  # never leak the sleeper on a failing (unfixed) tree
                    try:
                        os.kill(grand_pid, 9)
                    except ProcessLookupError:
                        pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
