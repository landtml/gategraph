"""Contract tests for the host event contract (core/hosts/bridge.py) and the model
port contract (core/agent/models.py).

Runs with the system python3 (no LangChain needed). The chat_model() test runs only
when langchain_core is importable, so it is skipped under the system python3.
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import json
import subprocess
import unittest

from core import brand
from core.kernel.types import Action, ActionKind, Spend, Verdict
from core.hosts import bridge
from core.agent import models

REPO = _bootstrap.REPO
EVENTS = REPO / "core" / "tests" / "hosts" / "events"

# Recorded sample event file -> the kind it must parse to.
EXPECTED_KINDS = {
    "pretool_write": "pre_tool",
    "pretool_edit": "pre_tool",
    "pretool_bash": "pre_tool",
    "posttool_write": "post_tool",
    "posttool_bash": "post_tool",
    "stop": "stop",
    "session_start": "session_start",
}


def _load_sample(name):
    return (EVENTS / f"{name}.json").read_text(encoding="utf-8")


def _has_langchain_core():
    try:
        import langchain_core  # noqa: F401
    except ImportError:
        return False
    return True


class TestHostEventSamples(unittest.TestCase):
    def test_sample_directory_lists_exactly_the_expected_files(self):
        names = sorted(p.stem for p in EVENTS.glob("*.json"))
        self.assertEqual(names, sorted(EXPECTED_KINDS))

    def test_every_sample_parses_to_its_kind(self):
        host = bridge.HOSTS["claude_code"]
        for name, kind in EXPECTED_KINDS.items():
            with self.subTest(sample=name):
                event = host.parse(_load_sample(name))
                self.assertIsInstance(event, bridge.HostEvent)
                self.assertEqual(event.kind, kind)
                self.assertIn(event.kind, bridge.EVENT_KINDS)

    def test_pretool_write_maps_to_write_action(self):
        event = bridge.HOSTS["claude_code"].parse(_load_sample("pretool_write"))
        self.assertEqual(event.session, "sample-session-01")
        self.assertEqual(event.tool, "Write")
        self.assertEqual(event.action,
                         Action(kind=ActionKind.WRITE, tool="Write",
                                path="/work/sample-repo/notes/todo.md",
                                content="hello\n"))
        self.assertIsInstance(event.raw, dict)
        self.assertEqual(event.raw["hook_event_name"], "PreToolUse")

    def test_pretool_edit_maps_new_string_to_content(self):
        event = bridge.HOSTS["claude_code"].parse(_load_sample("pretool_edit"))
        self.assertEqual(event.tool, "Edit")
        self.assertEqual(event.action.kind, ActionKind.EDIT)
        self.assertEqual(event.action.path, "/work/sample-repo/src/app.py")
        self.assertEqual(event.action.content, "def add(a, b):\n    return a + b\n")

    def test_pretool_bash_maps_to_exec_action(self):
        event = bridge.HOSTS["claude_code"].parse(_load_sample("pretool_bash"))
        self.assertEqual(event.tool, "Bash")
        self.assertEqual(event.action,
                         Action(kind=ActionKind.EXEC, tool="Bash", command="ls -la"))

    def test_posttool_events_keep_the_same_action_mapping(self):
        host = bridge.HOSTS["claude_code"]
        write = host.parse(_load_sample("posttool_write"))
        bash = host.parse(_load_sample("posttool_bash"))
        self.assertEqual(write.action.kind, ActionKind.WRITE)
        self.assertEqual(bash.action.kind, ActionKind.EXEC)
        self.assertEqual(bash.action.command, "python3 -m unittest")

    def test_stop_and_session_start_carry_no_action(self):
        host = bridge.HOSTS["claude_code"]
        stop = host.parse(_load_sample("stop"))
        start = host.parse(_load_sample("session_start"))
        self.assertIsNone(stop.action)
        self.assertIsNone(stop.tool)
        self.assertEqual(stop.session, "sample-session-01")
        self.assertIsNone(start.action)
        self.assertEqual(start.session, "sample-session-02")

    def test_unknown_event_name_is_rejected(self):
        payload = json.dumps({"hook_event_name": "Mystery", "session_id": "x"})
        with self.assertRaises(ValueError):
            bridge.HOSTS["claude_code"].parse(payload)

    def test_unknown_tool_has_no_action_but_keeps_the_tool_name(self):
        payload = json.dumps({"hook_event_name": "PreToolUse", "session_id": "x",
                              "tool_name": "Frobnicate", "tool_input": {}})
        event = bridge.HOSTS["claude_code"].parse(payload)
        self.assertEqual(event.tool, "Frobnicate")
        self.assertIsNone(event.action)

    def test_hostevent_rejects_an_unknown_kind(self):
        with self.assertRaises(ValueError):
            bridge.HostEvent(kind="whatever", session=None, tool=None, action=None, raw={})

    def test_registry_has_claude_code_and_each_host_is_a_host(self):
        self.assertIn("claude_code", bridge.HOSTS)
        self.assertIsInstance(bridge.HOSTS["claude_code"], bridge.Host)


class TestHostReplyRendering(unittest.TestCase):
    def setUp(self):
        self.host = bridge.HOSTS["claude_code"]

    def test_pre_tool_deny_is_a_json_permission_decision_on_exit_0(self):
        reply = bridge.HostReply(verdict=Verdict.DENY, reason="blocked by policy")
        text, code = self.host.render(reply, "pre_tool")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text), {"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": "blocked by policy"}})

    def test_pre_tool_ask_is_a_json_permission_decision_on_exit_0(self):
        reply = bridge.HostReply(verdict=Verdict.ASK, reason="confirm first")
        text, code = self.host.render(reply, "pre_tool")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_pre_tool_allow_is_silent_on_exit_0(self):
        text, code = self.host.render(bridge.HostReply(verdict=Verdict.ALLOW), "pre_tool")
        self.assertEqual((text, code), ("", 0))

    def test_post_tool_block_writes_reason_and_exits_2(self):
        reply = bridge.HostReply(verdict=Verdict.DENY, reason="fix the lint error", block=True)
        self.assertEqual(self.host.render(reply, "post_tool"), ("fix the lint error", 2))

    def test_post_tool_context_is_advisory_on_exit_0(self):
        reply = bridge.HostReply(verdict=Verdict.ALLOW, context="warning: long line")
        self.assertEqual(self.host.render(reply, "post_tool"), ("warning: long line", 0))

    def test_stop_block_is_a_json_block_decision_on_exit_0(self):
        reply = bridge.HostReply(verdict=Verdict.DENY, reason="write the handoff", block=True)
        text, code = self.host.render(reply, "stop")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text), {"decision": "block", "reason": "write the handoff"})

    def test_stop_without_block_is_silent(self):
        self.assertEqual(self.host.render(bridge.HostReply(verdict=Verdict.ALLOW), "stop"),
                         ("", 0))

    def test_session_start_context_is_plain_text_on_exit_0(self):
        reply = bridge.HostReply(verdict=Verdict.ALLOW, context="active unit: u1")
        self.assertEqual(self.host.render(reply, "session_start"), ("active unit: u1", 0))

    def test_reply_verdict_accepts_the_string_value(self):
        reply = bridge.HostReply(verdict="deny", reason="x")
        self.assertIs(reply.verdict, Verdict.DENY)


class TestFakeModel(unittest.TestCase):
    def script(self):
        return [
            "hello",
            models.Structured({"ok": True, "n": 2}),
            [models.ToolCall(name="read_file", args={"path": "a.md"}, id="c1")],
            "done",
        ]

    def test_text_step_is_returned_with_spend_and_no_data(self):
        fake = models.FakeModel(self.script())
        result = fake.complete("first prompt")
        self.assertIsInstance(result, models.ModelResult)
        self.assertEqual(result.text, "hello")
        self.assertIsNone(result.data)
        self.assertIsInstance(result.spend, Spend)
        self.assertEqual(result.spend.model_calls, 1)

    def test_structured_step_returns_data_and_its_json_text(self):
        fake = models.FakeModel(self.script())
        fake.complete("skip the text step")
        result = fake.complete("second", schema={"type": "object"})
        self.assertEqual(result.data, {"ok": True, "n": 2})
        self.assertEqual(json.loads(result.text), {"ok": True, "n": 2})

    def test_records_every_prompt_and_schema_it_was_given(self):
        fake = models.FakeModel(self.script())
        fake.complete("first prompt")
        fake.complete("second", schema={"type": "object"})
        self.assertEqual(fake.calls, ["first prompt", "second"])
        self.assertEqual(fake.schemas, [None, {"type": "object"}])

    def test_tool_step_is_replayed_through_chat_and_recorded(self):
        fake = models.FakeModel(self.script())
        fake.complete("skip")
        fake.complete("skip")
        messages = [{"role": "user", "content": "read a.md"}]
        turn = fake.chat(messages)
        self.assertIsInstance(turn, models.ChatTurn)
        self.assertEqual(turn.tool_calls,
                         [models.ToolCall(name="read_file", args={"path": "a.md"}, id="c1")])
        self.assertEqual(fake.chat([{"role": "user", "content": "next"}]).text, "done")
        self.assertEqual(fake.calls[2], messages)

    def test_tool_step_is_refused_by_complete_and_not_consumed(self):
        fake = models.FakeModel(self.script())
        fake.complete("skip")
        fake.complete("skip")
        with self.assertRaises(models.ScriptError):
            fake.complete("this should hit the tool step")
        turn = fake.chat([{"role": "user", "content": "still there"}])
        self.assertEqual(len(turn.tool_calls), 1)

    def test_exhausted_script_raises_a_clear_error(self):
        fake = models.FakeModel(["only"])
        fake.complete("one")
        with self.assertRaises(models.ScriptExhausted) as ctx:
            fake.complete("two")
        self.assertIn("script", str(ctx.exception))

    def test_supports_tools_and_protocol_conformance(self):
        fake = models.FakeModel([])
        self.assertTrue(fake.supports_tools)
        self.assertIsInstance(fake, models.ModelPort)

    @unittest.skipUnless(_has_langchain_core(), "langchain_core not installed")
    def test_chat_model_replays_the_same_script(self):
        from langchain_core.messages import HumanMessage
        fake = models.FakeModel(["hi", [models.ToolCall(name="read_file",
                                                        args={"path": "b.md"}, id="c9")]])
        model = fake.chat_model()
        first = model.invoke([HumanMessage(content="greet")])
        self.assertEqual(first.content, "hi")
        second = model.bind_tools([]).invoke([HumanMessage(content="read b")])
        self.assertEqual(second.tool_calls[0]["name"], "read_file")
        self.assertEqual(second.tool_calls[0]["args"], {"path": "b.md"})
        self.assertEqual(len(fake.calls), 2)


class TestBoundaries(unittest.TestCase):
    def test_bridge_and_models_import_without_langchain(self):
        code = ("import sys, core.hosts.bridge, core.agent.models as m\n"
                "assert not any(n.startswith('langchain') for n in sys.modules), 'eager import'\n"
                "print('clean')\n")
        out = subprocess.run([sys.executable, "-c", code], cwd=str(REPO),
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("clean", out.stdout)

    def test_product_name_is_absent_from_hosts_and_agent_sources(self):
        name = brand.NAME.lower()
        for path in (REPO / "core/hosts/bridge.py", REPO / "core/agent/models.py"):
            with self.subTest(file=path.name):
                self.assertNotIn(name, path.read_text(encoding="utf-8").lower())


if __name__ == "__main__":
    unittest.main()
