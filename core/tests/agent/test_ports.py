"""Model ports (A6): the claude_cli port (tool-less subprocess seam) and the api port.

No test spawns a subprocess or touches the network. The claude_cli port takes an
injected runner; the api port takes an injected chat-model factory that returns
FakeModel's scripted LangChain model.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import json
import shlex
import unittest

from core.agent.models import FakeModel, ModelPort, ModelResult
from core.agent.ports import (
    BudgetFault, ContractFault, ModelFault, RateLimitFault, SpawnFault,
)
from core.agent.ports.api import ApiPort
from core.agent.ports.claude_cli import (
    ClaudeCliPort, RunOutput, build_env, render_command,
)

SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"type": "string"}, "score": {"type": "integer"}},
    "required": ["verdict", "score"],
    "additionalProperties": False,
}


def envelope(result="ok", is_error=False, cost=0.01, usage=None, subtype="success"):
    return json.dumps({
        "type": "result", "subtype": subtype, "is_error": is_error,
        "result": result, "total_cost_usd": cost,
        "usage": usage if usage is not None else {"input_tokens": 4, "output_tokens": 2},
    })


class ScriptedRunner:
    """Returns the scripted RunOutputs in order and records every call."""

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    def __call__(self, argv, env, timeout_s):
        self.calls.append((list(argv), dict(env), timeout_s))
        out = self.outputs.pop(0)
        if isinstance(out, BaseException):
            raise out
        return out


def ok(stdout, stderr=""):
    return RunOutput(returncode=0, stdout=stdout, stderr=stderr)


class TestClaudeCliPort(unittest.TestCase):

    def test_supports_tools_is_false_and_is_a_model_port(self):
        port = ClaudeCliPort(runner=ScriptedRunner())
        self.assertFalse(port.supports_tools)
        self.assertIsInstance(port, ModelPort)

    def test_chat_model_is_refused_for_tool_less_port(self):
        with self.assertRaises(NotImplementedError):
            ClaudeCliPort(runner=ScriptedRunner()).chat_model()

    def test_B23_non_string_result_does_not_crash(self):
        for result in (None, {"a": 1}, ["x"], 7):
            runner = ScriptedRunner(ok(envelope(result=result)))
            res = ClaudeCliPort(runner=runner).complete("p")
            self.assertIsInstance(res, ModelResult)
            self.assertIsInstance(res.text, str)
            self.assertIsNone(res.data)

    def test_B23_non_mapping_envelope_is_a_spawn_fault(self):
        runner = ScriptedRunner(ok("[1, 2, 3]"))
        with self.assertRaises(SpawnFault):
            ClaudeCliPort(runner=runner).complete("p")

    def test_B24_limit_phrase_in_successful_output_is_not_a_rate_limit(self):
        artifact = "The report says: usage limit reached on the staging quota. 5-hour limit noted."
        runner = ScriptedRunner(ok(envelope(result=artifact, is_error=False)))
        res = ClaudeCliPort(runner=runner).complete("p")
        self.assertEqual(res.text, artifact)

    def test_B25_paths_with_spaces_are_quoted(self):
        path = "/home/user/My Projects/it's here/settings.json"
        argv = ["claude", "-p", "x", "--settings", path]
        rendered = render_command(argv)
        self.assertEqual(shlex.split(rendered), argv)
        # The port hands the runner a list, never a shell string.
        runner = ScriptedRunner(ok(envelope()))
        port = ClaudeCliPort(runner=runner, extra_args=("--settings", path))
        port.complete("x")
        sent = runner.calls[0][0]
        self.assertIn(path, sent)
        self.assertEqual(shlex.split(render_command(sent))[-1], path)

    def test_error_envelope_with_rate_limit_phrase_raises_retryable_fault(self):
        runner = ScriptedRunner(ok(envelope(result="API Error: 429 rate limit exceeded",
                                            is_error=True, cost=0.0)))
        with self.assertRaises(RateLimitFault) as cm:
            ClaudeCliPort(runner=runner).complete("p")
        self.assertTrue(cm.exception.retryable)
        self.assertIsInstance(cm.exception, ModelFault)

    def test_hard_limit_banner_in_stderr_raises_rate_limit(self):
        runner = ScriptedRunner(ok(envelope(result="done"), stderr="You've hit your session limit"))
        with self.assertRaises(RateLimitFault):
            ClaudeCliPort(runner=runner).complete("p")

    def test_budget_subtype_raises_non_retryable_budget_fault(self):
        runner = ScriptedRunner(ok(envelope(result="", is_error=True, subtype="error_max_budget_usd",
                                            cost=0.5)))
        with self.assertRaises(BudgetFault) as cm:
            ClaudeCliPort(runner=runner).complete("p")
        self.assertFalse(cm.exception.retryable)
        self.assertAlmostEqual(cm.exception.spend.usd, 0.5)

    def test_spend_accumulates_across_schema_repair_attempts(self):
        bad = envelope(result='{"verdict": "pass"}', cost=0.01,
                       usage={"input_tokens": 10, "output_tokens": 5})
        good = envelope(result='{"verdict": "pass", "score": 3}', cost=0.02,
                        usage={"input_tokens": 7, "output_tokens": 3})
        runner = ScriptedRunner(ok(bad), ok(good))
        res = ClaudeCliPort(runner=runner).complete("tariff it", schema=SCHEMA)
        self.assertEqual(res.data, {"verdict": "pass", "score": 3})
        self.assertAlmostEqual(res.spend.usd, 0.03)
        self.assertEqual(res.spend.tokens, 25)
        self.assertEqual(res.spend.model_calls, 2)
        # The repair attempt carries the driver's feedback, not the bare prompt.
        self.assertIn("driver feedback", runner.calls[1][0][runner.calls[1][0].index("-p") + 1])

    def test_spend_is_kept_when_every_attempt_fails_the_schema(self):
        bad = envelope(result='{"verdict": "pass"}', cost=0.01)
        runner = ScriptedRunner(ok(bad), ok(bad), ok(bad))
        with self.assertRaises(ContractFault) as cm:
            ClaudeCliPort(runner=runner, max_strikes=3).complete("p", schema=SCHEMA)
        self.assertAlmostEqual(cm.exception.spend.usd, 0.03)
        self.assertEqual(cm.exception.spend.model_calls, 3)
        self.assertTrue(cm.exception.errors)

    def test_API_key_variables_are_removed_from_child_environment(self):
        # Dummy values, set inside the test only.
        base = {"ANTHROPIC_API_KEY": "dummy-key", "ANTHROPIC_AUTH_TOKEN": "dummy-token",
                "HOME": "/home/user"}
        env = build_env(base)
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertEqual(env["HOME"], "/home/user")
        self.assertIn("ANTHROPIC_API_KEY", base)  # the caller's mapping is not mutated

        runner = ScriptedRunner(ok(envelope()))
        ClaudeCliPort(runner=runner, base_env=base).complete("p")
        child_env = runner.calls[0][1]
        self.assertNotIn("ANTHROPIC_API_KEY", child_env)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", child_env)

    def test_V4_binary_and_model_are_passed_through_exactly(self):
        runner = ScriptedRunner(ok(envelope()))
        ClaudeCliPort(runner=runner, model="claude-haiku-5-5",
                      binary="/opt/cli/native-binary/claude").complete("hi")
        argv = runner.calls[0][0]
        self.assertEqual(argv[0], "/opt/cli/native-binary/claude")
        self.assertEqual(argv[argv.index("--model") + 1], "claude-haiku-5-5")

    def test_canonical_tool_less_argv(self):
        runner = ScriptedRunner(ok(envelope()))
        ClaudeCliPort(runner=runner, model="haiku", budget_usd=0.5).complete("hi")
        argv = runner.calls[0][0]
        self.assertEqual(argv[:2], ["claude", "-p"])
        self.assertIn("--output-format", argv)
        self.assertIn("0.50", argv)
        self.assertNotIn("--settings", argv)
        self.assertNotIn("--allowedTools", argv)


class TestApiPort(unittest.TestCase):

    def test_supports_tools_and_is_a_model_port(self):
        port = ApiPort("fake", factory=lambda *a, **k: FakeModel(["x"]).chat_model())
        self.assertTrue(port.supports_tools)
        self.assertIsInstance(port, ModelPort)

    def test_chat_model_comes_from_factory_and_is_cached(self):
        made = []

        def factory(name, **kwargs):
            made.append((name, kwargs))
            return FakeModel(["x"]).chat_model()

        port = ApiPort("some-model", model_provider="p", factory=factory, temperature=0)
        first = port.chat_model()
        self.assertIs(port.chat_model(), first)
        self.assertEqual(len(made), 1)
        self.assertEqual(made[0][0], "some-model")
        self.assertEqual(made[0][1], {"model_provider": "p", "temperature": 0})

    def test_complete_without_schema_returns_scripted_text(self):
        port = ApiPort("fake", factory=lambda *a, **k: FakeModel(["hello there"]).chat_model())
        res = port.complete("say hi")
        self.assertEqual(res.text, "hello there")
        self.assertIsNone(res.data)
        self.assertEqual(res.spend.model_calls, 1)


if __name__ == "__main__":
    unittest.main()
