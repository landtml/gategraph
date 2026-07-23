"""The adapter-config surface must be honest (F16/F30/F2/F31).

Every key the seed ships must have a consumer; every knob a consumer reads must
be discoverable in the seed; names must not lie about their units. These tests
pin that contract:

- F16: τ is a DISAGREEMENT threshold, not an error probability, the key is
  `escalation.disagreement_threshold`; the old `error_prob_threshold` name
  still works (config back-compat, one release).
- F30: dead keys (workflow.terminal_states, escalation.confidence_signal,
  self_consistency.aggregate) are gone from the seed; the two undocumented
  live knobs (loop stop.graduate_target, backstops.rate_limit_guard) are
  discoverable in the seed; Oracle subclasses need only implement run_gates.
- F2: `Oracle.proposal_space()` is consumed, the driver hands it to the
  gateway and ClaudeGateway inlines it into the maker prompt as the allowed
  cell vocabulary.
- F31: hook-context lint runs take a time budget so a slow adapter check
  degrades to an EXPLICIT advisory skip instead of the host silently killing
  the hook (checks longer than the budget belong in `make check` / gate
  producers, where CHECK_TIMEOUT still applies).
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

from fixtures import default_adapter, mkroot, write_adapter  # noqa: E402
from test_loop_driver import DriverBase, scripted_check  # noqa: E402
from test_loop_workers import MockRunner, envelope  # noqa: E402
from core import lint, config  # noqa: E402
from core.loop.gateway import ClaudeGateway  # noqa: E402
from core.loop.oracle import Oracle  # noqa: E402
from core.loop.schemas import GateResult  # noqa: E402

# In the template repo parents[2] is the repo root and seed/ exists; in a
# STAMPED project this file lives under harness/core/tests and there is no
# seed, the seed-surface assertions are template-only and skip there.
TEMPLATE = Path(__file__).resolve().parents[2]
_SEED = TEMPLATE / "seed"
HAS_SEED = _SEED.is_dir()
SEED_ADAPTER = ((_SEED / "adapter.yaml").read_text(encoding="utf-8")
                if HAS_SEED else "")
SEED_SETTINGS = (json.loads((_SEED / "dot_claude" / "settings.json")
                            .read_text(encoding="utf-8"))
                 if HAS_SEED else {})
template_only = unittest.skipUnless(HAS_SEED, "template repo only (no seed/ in stamps)")


# ── F16: the τ key says what it is ───────────────────────────────────────────

DISAGREE_CHECKS = {"c000": [
    {}, {"structurally_promising": False}, {"exhausted": True},
]}


class TestTauKeyRename(DriverBase):
    def test_disagreement_threshold_drives_escalation(self):
        drv = self.mkdriver(check_fn=scripted_check(dict(DISAGREE_CHECKS)), cfg={
            "self_consistency": {"enabled": True, "n": 3},
            "escalation": {"disagreement_threshold": 0.01}})
        drv.run(max_iterations=3)
        esc = list((Path(self.state_root) / "escalations").glob("ESCALATION_*.md"))
        self.assertTrue(esc, "the renamed τ key must drive escalation")

    def test_old_error_prob_threshold_still_works(self):
        # back-compat for adapters stamped before the rename (one release)
        drv = self.mkdriver(check_fn=scripted_check(dict(DISAGREE_CHECKS)), cfg={
            "self_consistency": {"enabled": True, "n": 3},
            "escalation": {"error_prob_threshold": 0.01}})
        drv.run(max_iterations=3)
        esc = list((Path(self.state_root) / "escalations").glob("ESCALATION_*.md"))
        self.assertTrue(esc, "the old key must keep working for stamped projects")

    @template_only
    def test_seed_ships_the_honest_name(self):
        self.assertIn("disagreement_threshold", SEED_ADAPTER)
        self.assertNotRegex(SEED_ADAPTER, r"(?m)^\s*error_prob_threshold:")


# ── F30: no dead keys, no undocumented live knobs ────────────────────────────

@template_only
class TestSeedSchemaHonesty(unittest.TestCase):
    def test_dead_keys_are_gone(self):
        for dead in ("terminal_states", "confidence_signal", "aggregate:"):
            self.assertNotIn(dead, SEED_ADAPTER,
                             f"seed ships {dead!r} but nothing consumes it")

    def test_live_knobs_are_discoverable(self):
        # both are read by driver.py and were documented nowhere
        self.assertIn("graduate_target", SEED_ADAPTER)
        self.assertIn("rate_limit_guard", SEED_ADAPTER)

    def test_config_drops_the_dead_attr(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = config.load(mkroot(Path(td)))
            self.assertFalse(hasattr(cfg, "terminal_states"),
                             "config exposes an attribute nothing reads")


class TestOracleMinimalContract(unittest.TestCase):
    def test_run_gates_is_the_only_required_method(self):
        class Minimal(Oracle):
            def run_gates(self, variant_path, cfg):
                return GateResult(variant_id="x", passes={}, metrics={},
                                  tripwires={})

        oracle = Minimal()  # abstract incumbent/proposal_space would raise here
        self.assertIsNone(oracle.incumbent())
        self.assertEqual(oracle.proposal_space(), [])


# ── F2: proposal_space is consumed, not dead ─────────────────────────────────

class SpacedOracle:
    def incumbent(self):
        return None

    def run_gates(self, variant_path, cfg):
        return GateResult(variant_id="c000", passes={"quality": True},
                          metrics={}, tripwires={})

    def proposal_space(self):
        return ["alpha-mechanism", "beta-mechanism"]


class TestProposalSpaceConsumed(DriverBase):
    def test_driver_hands_the_space_to_the_gateway(self):
        seen = {}

        def capture_propose(*, variant_id, variant_dir, **kw):
            from core.loop.gateway import default_mock_propose
            seen.update(kw)
            return default_mock_propose(variant_id=variant_id,
                                        variant_dir=variant_dir,
                                        brief=kw.get("brief", ""))

        drv = self.mkdriver(propose_fn=capture_propose, oracle=SpacedOracle())
        drv.run(max_iterations=1)
        self.assertEqual(seen.get("proposal_space"),
                         ["alpha-mechanism", "beta-mechanism"],
                         "the oracle's declared cells never reach the maker")

    def test_claude_gateway_inlines_the_cells_into_the_maker_prompt(self):
        card = json.dumps({"id": "c1", "hypothesis": "h", "mechanism": "m",
                           "cell": "alpha-mechanism", "kill_criteria": "k",
                           "dead_end_citations": ["brief:L1"], "params": {}})
        runner = MockRunner([(0, envelope(card), "")])
        gw = ClaudeGateway(model="haiku", runner=runner)
        with tempfile.TemporaryDirectory() as td:
            cand = Path(td) / "c1"
            cand.mkdir()
            (cand / "variant.py").write_text("X = 1\n")
            gw.propose(variant_id="c1", variant_dir=str(cand), brief="b",
                       proposal_space=["alpha-mechanism", "beta-mechanism"])
        prompt = runner.calls[0]["argv"][2]
        self.assertIn("alpha-mechanism", prompt)
        self.assertIn("beta-mechanism", prompt)


# ── F31: hook-context time budget ────────────────────────────────────────────

class TestHookCheckBudget(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        adapter = default_adapter()
        adapter["checks"] = [{"id": "slowcheck", "cmd": "sleep 30",
                              "on": ["**/*.md"]}]
        self.root = mkroot(Path(self._tmp.name), adapter)

    def tearDown(self):
        self._tmp.cleanup()

    def test_exhausted_budget_skips_with_an_explicit_warn(self):
        cfg = config.load(self.root)
        f = self.root / "notes" / "u1" / "artifact.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("hello\n")
        t0 = time.monotonic()
        fatal, warn = lint.run_adapter_checks(self.root, cfg, [f], "u1",
                                              budget_s=0)
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(fatal, [], "a budget skip must not block the write")
        self.assertTrue(any("slowcheck" in w and "make check" in w for w in warn),
                        f"skip must be explicit and name the full lane: {warn}")

    def test_remaining_budget_caps_the_check_timeout(self):
        cfg = config.load(self.root)
        f = self.root / "notes" / "u1" / "artifact.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("hello\n")
        t0 = time.monotonic()
        fatal, warn = lint.run_adapter_checks(self.root, cfg, [f], "u1",
                                              budget_s=2)
        self.assertLess(time.monotonic() - t0, 10,
                        "the budget must cap the per-check timeout")
        self.assertTrue(any("slowcheck" in x for x in fatal + warn),
                        "a check cut off mid-run must be reported")

    def test_posttool_hook_honors_the_budget_env(self):
        hook = TEMPLATE / "core" / "hooks" / "posttool_lint.py"
        f = self.root / "notes" / "u1" / "artifact.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("hello\n")
        event = json.dumps({"session_id": "s1", "hook_event_name": "PostToolUse",
                            "tool_name": "Write",
                            "tool_input": {"file_path": str(f)}})
        env = dict(os.environ, CLAUDE_PROJECT_DIR=str(self.root),
                   FH_HOOK_CHECK_BUDGET="1")
        t0 = time.monotonic()
        r = subprocess.run([sys.executable, str(hook)], input=event,
                           capture_output=True, text=True, env=env,
                           cwd=self.root, timeout=60)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 10,
                        f"hook ran {elapsed:.0f}s, the host would have killed it")
        self.assertEqual(r.returncode, 0, r.stderr)


@template_only
class TestSeedHookTimeouts(unittest.TestCase):
    def test_posttool_timeout_clears_the_check_budget(self):
        post = SEED_SETTINGS["hooks"]["PostToolUse"]
        for entry in post:
            for h in entry["hooks"]:
                self.assertGreaterEqual(
                    h.get("timeout", 0), 90,
                    "PostToolUse timeout must exceed the hook check budget")

    def test_stop_timeout_clears_handoffs_own_subprocess(self):
        stop = SEED_SETTINGS["hooks"]["Stop"]
        for entry in stop:
            for h in entry["hooks"]:
                self.assertGreaterEqual(h.get("timeout", 0), 30)


@template_only
class TestThreatModelSurface(unittest.TestCase):
    """Part 1 item 4: the threat model must be written down, and the seed must
    point (not build) at real secret-scanning tooling for high-stakes projects."""

    def test_threat_model_doc_exists_and_states_both_sides(self):
        doc = TEMPLATE / "docs" / "THREAT-MODEL.md"
        self.assertTrue(doc.is_file(), "docs/THREAT-MODEL.md is the written threat model")
        text = doc.read_text(encoding="utf-8").lower()
        for phrase in ("fail open", "fail closed", "does not defend"):
            self.assertIn(phrase, text, f"threat model must state: {phrase!r}")

    def test_seed_points_at_secret_scanning_tooling(self):
        self.assertIn("gitleaks", SEED_ADAPTER,
                      "seed must show how to wire real secret scanning (advisory)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
