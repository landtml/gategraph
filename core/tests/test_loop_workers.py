"""Layer 2 worker-seam tests, everything that touches `claude -p` semantics,
driven through an injected MockRunner (no subprocess, no network, no spend),
plus the write_guard hook (subprocess, piped JSON, exactly as Claude Code
drives it) and the oracle loader.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.loop import contracts, workers  # noqa: E402
from core.loop.gateway import ClaudeGateway, MockGateway  # noqa: E402
from core.loop.oracle import Oracle, load_oracle  # noqa: E402
from core.loop.toy_oracle import ToyOracle  # noqa: E402

CORE = Path(__file__).resolve().parents[1]
GUARD = CORE / "hooks" / "write_guard.py"


def envelope(result, *, is_error=False, subtype="success", cost=0.01,
             denials=None, usage=None):
    return json.dumps({
        "result": result, "is_error": is_error, "subtype": subtype,
        "total_cost_usd": cost, "usage": usage or {"input_tokens": 10,
                                                   "output_tokens": 5},
        "permission_denials": denials or [], "session_id": "s", "num_turns": 1,
    })


class MockRunner:
    """Scripted runner: pops one (rc, stdout, stderr) per invocation."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def __call__(self, argv, env, timeout_s):
        self.calls.append({"argv": argv, "env": env, "timeout_s": timeout_s})
        rc, out, err = self.outputs.pop(0)
        return workers.RunOutput(rc, out, err)


class TestArgvEnv(unittest.TestCase):
    def test_canonical_argv_shape(self):
        spec = workers.WorkerSpec(prompt="p", model="haiku",
                                  json_schema={"type": "object"},
                                  settings_path="/tmp/s.json",
                                  write_scope="/tmp/scope")
        argv = workers.build_argv(spec)
        text = " ".join(argv)
        for needle in ("--output-format json", "--model haiku", "--json-schema",
                       "--permission-mode", "--setting-sources",
                       "--settings /tmp/s.json", "--strict-mcp-config",
                       "--disable-slash-commands", "--no-session-persistence",
                       "--max-budget-usd"):
            self.assertIn(needle, text)

    def test_env_scrubs_api_key_and_sets_scope(self):
        spec = workers.WorkerSpec(prompt="p", write_scope="/tmp/scope")
        env = workers.build_env(spec, base={"ANTHROPIC_API_KEY": "sk-x",
                                            "ANTHROPIC_AUTH_TOKEN": "t",
                                            "PATH": "/bin"})
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertTrue(env["HARNESS_WRITE_SCOPE"].endswith("scope"))


class TestInvoke(unittest.TestCase):
    def spec(self, **kw):
        return workers.WorkerSpec(prompt="p", **kw)

    def test_no_stdout_is_spawn_error(self):
        with self.assertRaises(workers.SpawnError):
            workers.invoke(self.spec(), MockRunner([(1, "", "boom")]))

    def test_non_json_stdout_is_spawn_error(self):
        with self.assertRaises(workers.SpawnError):
            workers.invoke(self.spec(), MockRunner([(0, "not json", "")]))

    def test_budget_subtype_raises_budget_exceeded(self):
        out = envelope("...", is_error=True, subtype="error_max_budget_usd")
        with self.assertRaises(workers.BudgetExceeded):
            workers.invoke(self.spec(), MockRunner([(0, out, "")]))

    def test_hard_limit_banner_on_success_envelope_is_rate_limited(self):
        out = envelope("You've hit your session limit · resets 3pm")
        with self.assertRaises(workers.RateLimited):
            workers.invoke(self.spec(), MockRunner([(0, out, "")]))

    def test_auth_marker_on_error_is_auth_error(self):
        out = envelope("Please run /login", is_error=True, subtype="error")
        with self.assertRaises(workers.AuthError):
            workers.invoke(self.spec(), MockRunner([(0, out, "")]))

    def test_success_parses_payload_and_usage(self):
        payload = {"decision": "DISTINCT", "confidence": 1.0, "reason": "r"}
        out = envelope(json.dumps(payload), cost=0.02)
        res = workers.invoke(self.spec(json_schema=contracts.JUDGE_CONTRACT),
                             MockRunner([(0, out, "")]))
        self.assertEqual(res.data, payload)
        self.assertEqual(res.cost_usd, 0.02)


class TestRunContract(unittest.TestCase):
    def test_success_first_try(self):
        payload = {"decision": "DISTINCT", "confidence": 1.0, "reason": "r"}
        runner = MockRunner([(0, envelope(json.dumps(payload)), "")])
        data, res = workers.run_contract(workers.WorkerSpec(prompt="p"),
                                         contracts.JUDGE_CONTRACT, runner)
        self.assertEqual(data["decision"], "DISTINCT")

    def test_schema_violations_strike_with_feedback_then_contract_error(self):
        bad = envelope(json.dumps({"decision": "MAYBE", "confidence": 2,
                                   "reason": "r"}))
        runner = MockRunner([(0, bad, "")] * 3)
        with self.assertRaises(workers.ContractError):
            workers.run_contract(workers.WorkerSpec(prompt="base"),
                                 contracts.JUDGE_CONTRACT, runner, max_strikes=3)
        self.assertEqual(len(runner.calls), 3)
        # attempts 2+ carry the concrete validation errors back to the worker
        retry_prompt = runner.calls[1]["argv"][2]
        self.assertIn("driver feedback", retry_prompt)

    def test_infra_error_propagates_without_striking(self):
        out = envelope("x", is_error=True, subtype="error_max_budget_usd")
        runner = MockRunner([(0, out, "")])
        with self.assertRaises(workers.BudgetExceeded):
            workers.run_contract(workers.WorkerSpec(prompt="p"),
                                 contracts.JUDGE_CONTRACT, runner)
        self.assertEqual(len(runner.calls), 1)


class TestSettingsFile(unittest.TestCase):
    def test_settings_wires_write_guard_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = workers.write_settings_file(str(Path(tmp) / "s.json"))
            data = json.loads(Path(path).read_text())
            cmd = data["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
            self.assertIn("write_guard.py", cmd)
            self.assertIn(str(GUARD), cmd)


class TestWriteGuardHook(unittest.TestCase):
    """Drive the hook as a real subprocess with JSON on stdin (fail-closed)."""

    def run_guard(self, event, scope):
        import os
        env = dict(os.environ)
        env.pop("HARNESS_WRITE_SCOPE", None)
        if scope is not None:
            env["HARNESS_WRITE_SCOPE"] = scope
        return subprocess.run([sys.executable, str(GUARD)],
                              input=json.dumps(event) if isinstance(event, dict)
                              else event,
                              capture_output=True, text=True, env=env)

    def test_in_scope_write_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.run_guard({"tool_name": "Write",
                                "tool_input": {"file_path": f"{tmp}/c/x.py"}}, tmp)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_out_of_scope_write_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.run_guard({"tool_name": "Write",
                                "tool_input": {"file_path": "/etc/passwd"}}, tmp)
            self.assertEqual(r.returncode, 2)

    def test_unset_scope_fails_closed(self):
        r = self.run_guard({"tool_name": "Write",
                            "tool_input": {"file_path": "/tmp/x"}}, None)
        self.assertEqual(r.returncode, 2)

    def test_parent_escape_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.run_guard({"tool_name": "Edit",
                                "tool_input": {"file_path": f"{tmp}/../evil.py"}},
                               tmp)
            self.assertEqual(r.returncode, 2)

    def test_non_mutator_allowed(self):
        r = self.run_guard({"tool_name": "Read",
                            "tool_input": {"file_path": "/etc/passwd"}}, None)
        self.assertEqual(r.returncode, 0)

    def test_garbage_payload_fails_closed(self):
        r = self.run_guard("not json{{", "/tmp")
        self.assertEqual(r.returncode, 2)


class TestGateways(unittest.TestCase):
    def test_mock_gateway_defaults_write_variant_and_graduate_worthy_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            gw = MockGateway()
            res = gw.propose(variant_id="c000", variant_dir=tmp, brief="b")
            self.assertTrue(Path(res.experiment_path).exists())
            self.assertEqual(res.card.id, "c000")
            check = gw.check(variant_id="c000",
                             experiment_path=res.experiment_path, gate_result={})
            self.assertTrue(check.judgement["gaming_clean"])
            judge = gw.judge(card=res.card, neighbours=[])
            self.assertEqual(judge.decision, "DISTINCT")

    def test_claude_gateway_propose_missing_variant_is_smoke_error(self):
        from core.loop.errors import SmokeError
        payload = {"id": "c1", "hypothesis": "h", "mechanism": "m", "cell": "x",
                   "kill_criteria": "k", "dead_end_citations": ["brief:none"]}
        runner = MockRunner([(0, envelope(json.dumps(payload)), "")])
        with tempfile.TemporaryDirectory() as tmp:
            gw = ClaudeGateway(model="haiku", runner=runner)
            with self.assertRaises(SmokeError):
                gw.propose(variant_id="c1", variant_dir=str(Path(tmp) / "c1"),
                           brief="b")

    def test_claude_gateway_check_inlines_artifacts(self):
        judgement = {"variant_id": "c1",
                     "soft_passes": {"mechanism_lead": True, "robustness": True},
                     "gaming_clean": True, "mechanism_sentence": "m",
                     "structurally_promising": True, "exhausted": False}
        runner = MockRunner([(0, envelope(json.dumps(judgement)), "")])
        with tempfile.TemporaryDirectory() as tmp:
            cand = Path(tmp) / "variant.py"
            cand.write_text("VALUE = 1\n")
            gw = ClaudeGateway(model="haiku", runner=runner)
            res = gw.check(variant_id="c1", experiment_path=str(cand),
                           gate_result={"passes": {"a": True}})
            self.assertTrue(res.judgement["gaming_clean"])
            prompt = runner.calls[0]["argv"][2]
            self.assertIn("VALUE = 1", prompt)      # variant code inlined
            self.assertIn('"a": true', prompt)      # locked gate result inlined


class TestOracleLoading(unittest.TestCase):
    def test_toy_oracle_satisfies_the_abc(self):
        toy = ToyOracle()
        self.assertIsInstance(toy, Oracle)
        self.assertTrue(toy.proposal_space())
        self.assertIsInstance(toy.incumbent(), dict)

    def test_load_oracle_core_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            oracle = load_oracle("core.loop.toy_oracle:ToyOracle", Path(tmp))
            self.assertEqual(type(oracle).__name__, "ToyOracle")

    def test_load_oracle_project_file_spec(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "harness" / "adapter").mkdir(parents=True)
            (root / "harness" / "adapter" / "oracle.py").write_text(
                "class PilotOracle:\n"
                "    def incumbent(self): return {}\n"
                "    def run_gates(self, path, cfg): raise NotImplementedError\n"
                "    def proposal_space(self): return ['a']\n")
            oracle = load_oracle("harness.adapter.oracle:PilotOracle", root)
            self.assertEqual(oracle.proposal_space(), ["a"])

    def test_load_oracle_missing_file_raises_helpfully(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(Exception) as ctx:
                load_oracle("harness.adapter.oracle:Oracle", Path(tmp))
            self.assertIn("oracle", str(ctx.exception).lower())


class TestToyOracle(unittest.TestCase):
    def write(self, tmp, text):
        p = Path(tmp) / "c000" / "variant.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return str(p)

    def test_default_variant_passes_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            gr = ToyOracle().run_gates(self.write(tmp, "OK = True\n"), {})
            self.assertTrue(all(gr.passes.values()))
            self.assertTrue(gr.tripwire_ok)
            self.assertEqual(gr.variant_id, "c000")

    def test_scripted_outcome_attributes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, "TOY_PASSES = {'quality': False}\n"
                                   "TOY_TRIPWIRES = {'leak': True}\n"
                                   "TOY_METRICS = {'score': 0.1}\n")
            gr = ToyOracle().run_gates(path, {})
            self.assertFalse(gr.passes["quality"])
            self.assertFalse(gr.tripwire_ok)
            self.assertEqual(gr.metrics["score"], 0.1)

    def test_broken_variant_raises_to_the_infra_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, "raise RuntimeError('scripted oracle fault')\n")
            with self.assertRaises(RuntimeError):
                ToyOracle().run_gates(path, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
