"""A7: NativeActor.run, the agent loop as the native work actor (plan 1.11; brief A7).

Every model is a FakeModel. Networking is disabled in this process: socket connects, name
lookups and create_connection raise, so any network attempt fails the test. Run it under
``unshare -rn .venv/bin/python core/tests/agent/test_loop.py`` for the strict form.

Checks:
  * the loop ends when the model calls ``request_gate``, with no further model call;
  * a model-call ceiling and a tool-call ceiling end the loop with an unfinished result,
    and the reason is a fixed phrase, never message text;
  * a refused write reaches the model as a tool message and never reaches disk;
  * the toolbox holds the packs' grants plus the harness tools, nothing else;
  * the result carries no transcript text, and ``finished``/``gate`` follow amendment 5;
  * the actor registers under the name ``native`` and satisfies ``WorkActor``.
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import json
import socket
import tempfile
import unittest
from pathlib import Path

from langchain_core.messages import SystemMessage, ToolMessage
from langgraph.errors import GraphInterrupt


def _refuse_network(*args, **kwargs):
    raise AssertionError("network access attempted from the agent tests")


socket.socket.connect = _refuse_network
socket.socket.connect_ex = _refuse_network
socket.create_connection = _refuse_network
socket.getaddrinfo = _refuse_network

from core.agent import loop  # noqa: E402
from core.packs.ops.tool import apply_spec  # noqa: E402
from core.agent.models import FakeModel, ToolCall  # noqa: E402
from core.graph.contracts import WorkActor, WorkResult  # noqa: E402
from core.graph.nodes.work import lookup_work_actor  # noqa: E402
from core.kernel.config import Config  # noqa: E402
from core.kernel.types import Action, ActionKind, Budget, GateSpec, Spend  # noqa: E402
from core.tools.spec import ToolResult, ToolSpec  # noqa: E402

SECRET = "PLANTED-SECRET-MARKER"
POLICY = {"id": "fake-secret", "regex": SECRET, "severity": "fatal",
          "scope": ["any"], "summary": "fake secret marker"}
TRANSCRIPT = "TRANSCRIPT-MARKER-NOTE"
PACK = "pack: sketching\nversion: 1\ntools: [read_file, glob, grep, write_file, edit_file]\n"


def call(name, args, cid):
    return [ToolCall(name=name, args=args, id=cid)]


def gate_call(gate, cid="gate1"):
    return call("request_gate", {"gate": gate, "note": "done"}, cid)


def make_config(root, *, packs=("sketching",), deny=(POLICY,)):
    if packs:
        pack_dir = Path(root) / "packs" / packs[0]
        pack_dir.mkdir(parents=True, exist_ok=True)
        (pack_dir / "pack.yaml").write_text(PACK, encoding="utf-8")
    return Config(root=Path(root), schema=2, project="proj", unit_noun="report",
                  unit_dir="units", packs=tuple(packs), checks=(),
                  gates=(GateSpec(id="g1"),), deny=tuple(deny))


class ActorTest(unittest.TestCase):
    """Shared fixture: one temporary project, one unit directory, one actor factory."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.unit_dir = self.root / "units" / "u1"
        self.unit_dir.mkdir(parents=True)
        self.state_dir = self.root / "state"
        self.budget = Budget(max_iterations=50, max_model_calls=20, max_tool_calls=40)

    def actor(self, script, *, config=None, budget=None, **tool_kwargs):
        model = FakeModel(script)
        actor = loop.NativeActor(config or make_config(self.root), model,
                                 unit_dir=self.unit_dir, state_dir=self.state_dir,
                                 budget=budget or self.budget, **tool_kwargs)
        return actor, model

    def run_goal(self, actor, gate="g1", findings=()):
        context = {"project": "proj", "unit": "u1", "gate": gate}
        return actor.run("Write the sketch.", list(findings), context)


class RequestGateEndsLoopTest(ActorTest):

    def test_request_gate_ends_the_loop_with_no_further_model_call(self):
        actor, model = self.actor([gate_call("g1")])
        result = self.run_goal(actor)
        self.assertIsInstance(result, WorkResult)
        self.assertTrue(result.finished)
        self.assertEqual(result.gate, "g1")
        self.assertEqual(len(model.calls), 1)

    def test_summary_names_the_gate_and_carries_no_transcript(self):
        actor, _ = self.actor([call("note_decision", {"text": TRANSCRIPT}, "n1"),
                               gate_call("g1")])
        result = self.run_goal(actor)
        self.assertEqual(result.summary, "request_gate: gate g1")
        self.assertNotIn(TRANSCRIPT, result.summary)

    def test_spend_counts_model_and_tool_calls(self):
        actor, model = self.actor([call("read_file", {"path": "units/u1/none.md"}, "r1"),
                                   gate_call("g1")])
        result = self.run_goal(actor)
        self.assertIsInstance(result.spend, Spend)
        self.assertEqual(result.spend.model_calls, 2)
        self.assertEqual(result.spend.tool_calls, 2)
        self.assertEqual(len(model.calls), 2)

    def test_goal_and_findings_reach_the_model(self):
        actor, model = self.actor([gate_call("g1")])
        finding = {"rule": "lint.unused", "severity": "fatal", "path": None, "line": None,
                   "message": "FINDING-TEXT-FROM-LAST-ATTEMPT"}
        self.run_goal(actor, findings=[finding])
        first = model.calls[0]
        system = [m.content for m in first if isinstance(m, SystemMessage)]
        flat = json.dumps([m.content for m in first if hasattr(m, "content")], default=str)
        self.assertTrue(system)
        self.assertIn("Write the sketch.", flat)
        self.assertIn("FINDING-TEXT-FROM-LAST-ATTEMPT", flat)


class CeilingTest(ActorTest):

    def test_model_call_ceiling_ends_with_an_unfinished_result(self):
        script = [call("read_file", {"path": "units/u1/a.md"}, "r1"),
                  call("read_file", {"path": "units/u1/b.md"}, "r2"),
                  gate_call("g1")]
        actor, model = self.actor(script, budget=Budget(max_iterations=50, max_model_calls=2))
        result = self.run_goal(actor)
        self.assertFalse(result.finished)
        self.assertIsNone(result.gate)
        self.assertIn("model-call ceiling", result.summary)
        self.assertEqual(len(model.calls), 2)

    def test_tool_call_ceiling_ends_with_an_unfinished_result(self):
        script = [call("read_file", {"path": "units/u1/a.md"}, "r1"),
                  call("read_file", {"path": "units/u1/b.md"}, "r2"),
                  gate_call("g1")]
        actor, _ = self.actor(script, budget=Budget(max_iterations=50, max_tool_calls=1))
        result = self.run_goal(actor)
        self.assertFalse(result.finished)
        self.assertIn("tool-call ceiling", result.summary)
        self.assertLessEqual(result.spend.tool_calls, 1)

    def test_reply_without_request_gate_is_unfinished(self):
        actor, _ = self.actor(["I have considered the sketch and I am finished."])
        result = self.run_goal(actor)
        self.assertFalse(result.finished)
        self.assertIsNone(result.gate)
        self.assertNotIn("considered", result.summary)


class ToolsAndGrantsTest(ActorTest):

    def test_refused_write_reaches_the_model_and_never_reaches_disk(self):
        script = [call("write_file", {"path": "units/u1/report.md",
                                      "content": f"sketch {SECRET}"}, "w1"),
                  call("write_file", {"path": "units/u1/report.md",
                                      "content": "clean sketch"}, "w2"),
                  gate_call("g1")]
        actor, model = self.actor(script)
        result = self.run_goal(actor)
        self.assertTrue(result.finished)
        seen = [m for m in model.calls[1] if isinstance(m, ToolMessage)]
        self.assertTrue(any("fake-secret" in m.content for m in seen), seen)
        self.assertEqual((self.unit_dir / "report.md").read_text(encoding="utf-8"),
                         "clean sketch")
        self.assertFalse(any(SECRET in p.read_text(encoding="utf-8", errors="replace")
                             for p in self.unit_dir.rglob("*") if p.is_file()))

    def test_tool_outside_the_packs_grants_is_unknown(self):
        config = make_config(self.root, packs=())
        script = [call("write_file", {"path": "units/u1/report.md", "content": "x"}, "w1"),
                  gate_call("g1")]
        actor, model = self.actor(script, config=config)
        result = self.run_goal(actor)
        self.assertTrue(result.finished)
        seen = [m for m in model.calls[1] if isinstance(m, ToolMessage)]
        self.assertTrue(any("unknown-tool" in m.content for m in seen), seen)
        self.assertFalse((self.unit_dir / "report.md").exists())

    def test_grants_are_the_pack_tools_plus_harness_tools(self):
        actor, _ = self.actor([])
        granted = actor.grants()
        self.assertIn("write_file", granted)
        self.assertIn("request_gate", granted)
        self.assertNotIn("fetch_url", granted)
        self.assertNotIn("run_command", granted)


class ApprovalTest(ActorTest):
    """A needs_approval tool inside the native agent pauses, it is never swallowed. The nested
    agent's interrupt is re-raised as a GraphInterrupt, so a unit graph pauses on it (see
    test_end_to_end.py). Run on its own, with no graph around it, nothing can answer it, so the
    interrupt reaches the caller. The tool body does not run."""

    def test_needs_approval_tool_interrupt_propagates_and_the_body_does_not_run(self):
        ran = []
        spec = ToolSpec(
            name="risky", description="needs approval",
            args_schema={"type": "object", "properties": {}, "required": [],
                         "additionalProperties": False},
            effects=frozenset({"read"}),
            to_action=lambda args: Action(kind=ActionKind.READ, tool="risky"),
            run=lambda args, ctx: (ran.append(1), ToolResult(content="ran"))[1],
            idempotent=True, needs_approval=True)
        granted = loop.NativeActor.grants
        loop.NativeActor.grants = lambda self: granted(self) | {"risky"}
        self.addCleanup(setattr, loop.NativeActor, "grants", granted)

        actor, _ = self.actor([call("risky", {}, "r1"), gate_call("g1")],
                              pack_tools={"risky": lambda: spec})
        with self.assertRaises(GraphInterrupt) as raised:
            self.run_goal(actor)
        self.assertEqual(raised.exception.args[0][0].value["tool"], "risky")
        self.assertEqual(ran, [])


class RegistrationTest(ActorTest):

    def test_registers_under_native_and_satisfies_work_actor(self):
        actor, _ = self.actor([])
        self.assertIsInstance(actor, WorkActor)
        loop.register_native(actor)
        self.assertIs(lookup_work_actor("native"), actor)


class UnavailableToolTest(ActorTest):
    """A granted tool that cannot be built is named in the work prompt and in the summary."""

    RUN_PACK = ("pack: sketching\nversion: 1\n"
                "tools: [read_file, glob, grep, write_file, edit_file, run_command]\n")

    def test_granted_but_unavailable_tool_is_reported(self):
        config = make_config(self.root)
        (self.root / "packs" / "sketching" / "pack.yaml").write_text(self.RUN_PACK,
                                                                    encoding="utf-8")
        actor, model = self.actor([gate_call("g1")], config=config)
        result = self.run_goal(actor)
        self.assertTrue(result.finished)
        first = model.calls[0]
        flat = json.dumps([m.content for m in first if hasattr(m, "content")], default=str)
        self.assertIn("run_command", flat)
        self.assertIn("allow-list", flat)
        self.assertIn("run_command", result.summary)
        self.assertIn("allow-list", result.summary)

    def test_pack_tool_is_granted_and_held_when_supplied(self):
        config = make_config(self.root)
        (self.root / "packs" / "sketching" / "pack.yaml").write_text(
            "pack: sketching\nversion: 1\ntools: [read_file, apply_plan]\n", encoding="utf-8")
        actor, model = self.actor([gate_call("g1")], config=config,
                                  pack_tools={"apply_plan": apply_spec})
        self.assertIn("apply_plan", actor.grants())
        result = self.run_goal(actor)
        self.assertNotIn("unavailable", result.summary)
        first = model.calls[0]
        flat = json.dumps([m.content for m in first if hasattr(m, "content")], default=str)
        self.assertNotIn("unavailable", flat)


if __name__ == "__main__":
    unittest.main()
