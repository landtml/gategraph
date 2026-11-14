"""A7 exit: a three-gate unit walks to ``complete`` with the native actor (plan 1.5, 1.11).

The graph is the G7 unit graph. Its work steps are the native agent, a FakeModel that
replays a script; its approval is answered by this file; its verifier is a FakeModel
too. Networking is disabled in this process (see test_loop.py), and no real model is
called.

The script plants one policy violation in the first gate. The guard denies that write,
the model sees the denial as a tool message, writes a clean sketch, and the gate passes.
Run under ``unshare -rn .venv/bin/python core/tests/agent/test_end_to_end.py``.
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import hashlib
import json
import socket
import tempfile
import unittest
from pathlib import Path

from langchain_core.messages import ToolMessage


def _refuse_network(*args, **kwargs):
    raise AssertionError("network access attempted from the agent tests")


socket.socket.connect = _refuse_network
socket.socket.connect_ex = _refuse_network
socket.create_connection = _refuse_network
socket.getaddrinfo = _refuse_network

from langgraph.types import Command  # noqa: E402

from core.agent import loop  # noqa: E402
from core.agent.models import FakeModel, Structured, ToolCall  # noqa: E402
from core.graph import runner  # noqa: E402
from core.graph.nodes.verify import sha256_text  # noqa: E402
from core.graph.store import make_checkpointer  # noqa: E402
from core.graph.unit import build_unit_graph, initial_state  # noqa: E402
from core.kernel.config import Config  # noqa: E402
from core.kernel.types import (Action, ActionKind, Approval, Budget, CheckSpec,  # noqa: E402
                               GateSpec, RepairSpec, Severity, VerifierSpec)
from core.tools.spec import ToolResult, ToolSpec  # noqa: E402

THREAD = "proj/u1"
DIGEST = hashlib.sha256(b"fixture configuration").hexdigest()
APPROVE = {"answer": "approve", "by": "tester", "channel": "tty", "note": None}
SECRET = "PLANTED-SECRET-MARKER"
TRANSCRIPT = "TRANSCRIPT-MARKER-NOTE"
POLICY = {"id": "fake-secret", "regex": SECRET, "severity": "fatal",
          "scope": ["any"], "summary": "fake secret marker"}
PACK = "pack: sketching\nversion: 1\ntools: [read_file, glob, grep, write_file, edit_file]\n"
SKETCH = "units/u1/report.md"


def call(name, args, cid):
    return [ToolCall(name=name, args=args, id=cid)]


def gate_call(gate, cid):
    return call("request_gate", {"gate": gate, "note": "ready"}, cid)


# The agent's script for all three gates, consumed in order (7 model calls in all).
AGENT_SCRIPT = [
    # g1: a deliberate violation (denied), a decision note, then a clean sketch.
    call("write_file", {"path": SKETCH, "content": f"sketch {SECRET}"}, "g1-w-bad"),
    call("note_decision", {"text": TRANSCRIPT}, "g1-note"),
    call("write_file", {"path": SKETCH, "content": "sketch one\n"}, "g1-w-ok"),
    gate_call("g1", "g1-done"),
    # g2: read the sketch and ask for the gate. No write: a write inside g1's surface would
    # make g1 stale (reconcile), and the unit could not stay on g2.
    call("read_file", {"path": SKETCH}, "g2-read"),
    gate_call("g2", "g2-done"),
    # g3: nothing to change; ask for the gate.
    gate_call("g3", "g3-done"),
]


def gates_and_checks():
    gates = [
        GateSpec(id="g1", checks=("c1",), repair=RepairSpec(max_attempts=2)),
        GateSpec(id="g2", needs=("g1",), checks=("c2",),
                 approval=Approval(required=True, channel="tty")),
        GateSpec(id="g3", needs=("g2",), checks=("c3",),
                 verifiers=(VerifierSpec(id="g3.v1", rubric="rubric.md", n=3,
                                         model="reviewer"),)),
    ]
    checks = [CheckSpec(id=c, command="true", severity=Severity.FATAL, timeout_s=20.0)
              for c in ("c1", "c2", "c3")]
    return gates, checks


def pass_verdict():
    return Structured({"reasoning": "the sketch meets the rubric", "verdict": "pass",
                       "findings": []})


class ThreeGateUnitTest(unittest.TestCase):

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.unit_dir = self.root / "units" / "u1"
        self.unit_dir.mkdir(parents=True)
        self.state_dir = self.root / "state"
        self.lock_dir = self.root / "locks"
        pack_dir = self.root / "packs" / "sketching"
        pack_dir.mkdir(parents=True)
        (pack_dir / "pack.yaml").write_text(PACK, encoding="utf-8")
        gates, checks = gates_and_checks()
        self.config = Config(root=self.root, schema=2, project="proj", unit_noun="report",
                             unit_dir="units", packs=("sketching",), checks=tuple(checks),
                             gates=tuple(gates), deny=(POLICY,))
        self.agent = FakeModel(AGENT_SCRIPT)
        self.verifier = FakeModel([pass_verdict() for _ in range(3)])
        actor = loop.NativeActor(self.config, self.agent, unit_dir=self.unit_dir,
                                 state_dir=self.state_dir,
                                 budget=Budget(max_iterations=50, max_model_calls=20, max_tool_calls=40))
        loop.register_native(actor)

    def graph(self):
        def load_artifact(gate_id):
            path = self.unit_dir / "report.md"
            text = path.read_text(encoding="utf-8") if path.is_file() else ""
            return text, sha256_text(text)

        return build_unit_graph({
            "config": self.config,
            "unit_dir": self.unit_dir,
            "state_dir": self.state_dir,
            "config_digest": DIGEST,
            "budget": Budget(max_iterations=200, max_model_calls=100),
            "load_artifact": load_artifact,
            "load_rubric": lambda path: "RUBRIC: the sketch must be clear.",
            "model_ports": {"reviewer": self.verifier},
            "spread_threshold": 0.0,
            "checkpointer": make_checkpointer({"in_memory": True}),
        })

    def run_unit(self, graph):
        """Start the unit and answer each interrupt. Returns the interrupt kinds answered."""
        config = {"configurable": {"thread_id": THREAD}}
        start = initial_state(project="proj", unit="u1", config_digest=DIGEST,
                              actor="native")
        runner.run(graph, THREAD, start, lambda snapshot: None, lock_dir=self.lock_dir)
        answered = []
        for _ in range(20):
            items = runner.pending(graph, THREAD)
            if not items:
                break
            kind = items[0]["kind"]
            answered.append(kind)
            if kind != "approval":
                raise AssertionError(f"the native unit paused for {kind!r}: {items[0]}")
            runner.run(graph, THREAD, Command(resume=APPROVE), lambda snapshot: None,
                       lock_dir=self.lock_dir)
        self.assertEqual(graph.get_state(config).next, ())
        return answered

    def values(self, graph):
        return graph.get_state({"configurable": {"thread_id": THREAD}}).values

    def test_three_gate_unit_walks_to_complete(self):
        graph = self.graph()
        answered = self.run_unit(graph)
        self.assertEqual(answered, ["approval"])
        final = self.values(graph)
        self.assertEqual(final["status"], "complete")
        self.assertEqual(final["effective"], ["g1", "g2", "g3"])
        self.assertEqual([r["gate"] for r in final["evidence"]], ["g1", "g2", "g3"])

    def test_every_scripted_model_call_was_made_and_none_extra(self):
        graph = self.graph()
        self.run_unit(graph)
        self.assertEqual(len(self.agent.calls), len(AGENT_SCRIPT))

    def test_policy_violation_is_denied_repaired_then_passes(self):
        graph = self.graph()
        self.run_unit(graph)
        # The model's second call sees the denial of the planted write as a tool message.
        second = [m for m in self.agent.calls[1] if isinstance(m, ToolMessage)]
        denials = [m for m in second if m.name == "write_file" and m.status == "error"]
        self.assertTrue(any("fake-secret" in m.content for m in denials), second)
        # The denial is a repair inside one work step: no gate failed, nothing was attempted again.
        final = self.values(graph)
        self.assertFalse(final["attempts"])
        self.assertEqual(final["effective"], ["g1", "g2", "g3"])
        # The planted text never reached disk; the repaired sketch is what stays.
        self.assertEqual((self.unit_dir / "report.md").read_text(encoding="utf-8"),
                         "sketch one\n")
        for path in self.unit_dir.rglob("*"):
            if path.is_file():
                self.assertNotIn(SECRET, path.read_text(encoding="utf-8", errors="replace"))

    def test_unit_state_holds_no_message_text(self):
        graph = self.graph()
        self.run_unit(graph)
        text = json.dumps(self.values(graph), default=str, sort_keys=True)
        self.assertNotIn(SECRET, text)
        self.assertNotIn(TRANSCRIPT, text)
        self.assertEqual(self.values(graph)["work_summary"], "request_gate: gate g3")


PUBLISH_PACK = ("pack: sketching\nversion: 1\n"
                "tools: [read_file, glob, grep, write_file, edit_file, publish_note]\n")
NOTE = "units/u1/notes.md"


def publish_spec(runs):
    """A write tool that needs a human approval. Its body appends to ``runs`` each time it runs."""
    def run(args, ctx):
        runs.append(args["path"])
        (ctx.root / args["path"]).write_text(args["content"], encoding="utf-8")
        return ToolResult(content=f"published {args['path']}", touched=(args["path"],))

    return ToolSpec(
        name="publish_note", description="Publish a note (needs a human approval).",
        args_schema={"type": "object",
                     "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                     "required": ["path", "content"], "additionalProperties": False},
        effects=frozenset({"write"}),
        to_action=lambda args: Action(kind=ActionKind.WRITE, tool="publish_note",
                                      path=args["path"]),
        run=run, idempotent=True, needs_approval=True)


class NeedsApprovalPausesTheUnitTest(unittest.TestCase):
    """A tool that needs approval inside the native agent pauses the unit graph (a recorded design decision).

    The agent script is: call the approval tool, then request the gate. Model calls are counted
    across the pause and the proceed: one before the pause, one after (Q4). A full restart of the
    agent would make the second count two.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        self.unit_dir = self.root / "units" / "u1"
        self.unit_dir.mkdir(parents=True)
        self.state_dir = self.root / "state"
        self.lock_dir = self.root / "locks"
        pack_dir = self.root / "packs" / "sketching"
        pack_dir.mkdir(parents=True)
        (pack_dir / "pack.yaml").write_text(PUBLISH_PACK, encoding="utf-8")
        self.config = Config(
            root=self.root, schema=2, project="proj", unit_noun="report", unit_dir="units",
            packs=("sketching",),
            checks=(CheckSpec(id="c1", command="true", severity=Severity.FATAL,
                              timeout_s=20.0),),
            gates=(GateSpec(id="g1", checks=("c1",)),), deny=(POLICY,))
        self.runs = []
        self.agent = FakeModel([call("publish_note", {"path": NOTE, "content": "sketch"}, "p1"),
                                gate_call("g1", "g1-done")])
        actor = loop.NativeActor(self.config, self.agent, unit_dir=self.unit_dir,
                                 state_dir=self.state_dir,
                                 budget=Budget(max_iterations=50, max_model_calls=20,
                                               max_tool_calls=40),
                                 pack_tools={"publish_note": lambda: publish_spec(self.runs)})
        loop.register_native(actor)

    def graph(self):
        def load_artifact(gate_id):
            path = self.unit_dir / "notes.md"
            text = path.read_text(encoding="utf-8") if path.is_file() else ""
            return text, sha256_text(text)

        return build_unit_graph({
            "config": self.config, "unit_dir": self.unit_dir, "state_dir": self.state_dir,
            "config_digest": DIGEST,
            "budget": Budget(max_iterations=200, max_model_calls=100),
            "load_artifact": load_artifact,
            "checkpointer": make_checkpointer({"in_memory": True}),
        })

    def pause(self, graph):
        """Start the unit. It must stop at the approval request of the agent's tool."""
        start = initial_state(project="proj", unit="u1", config_digest=DIGEST, actor="native")
        runner.run(graph, THREAD, start, lambda snapshot: None, lock_dir=self.lock_dir)

    def proceed(self, graph, answer):
        runner.run(graph, THREAD, Command(resume=answer), lambda snapshot: None,
                   lock_dir=self.lock_dir)

    def test_needs_approval_tool_pauses_the_unit(self):
        graph = self.graph()
        self.pause(graph)
        items = runner.pending(graph, THREAD)
        self.assertEqual(len(items), 1, items)
        self.assertEqual(items[0]["tool"], "publish_note")
        self.assertEqual(items[0]["kind"], "ask")
        self.assertEqual(items[0]["path"], NOTE)
        self.assertEqual(self.runs, [], "nothing may run before the human answers")
        self.assertEqual(len(self.agent.calls), 1, "one model call before the pause")
        self.assertIn("work", graph.get_state({"configurable": {"thread_id": THREAD}}).next)

    def test_approval_allow_runs_tool_once_and_finishes(self):
        graph = self.graph()
        self.pause(graph)
        self.proceed(graph, "allow")
        self.assertEqual(runner.pending(graph, THREAD), [])
        self.assertEqual(self.runs, [NOTE], "after allow the tool body ran exactly once")
        self.assertEqual((self.unit_dir / "notes.md").read_text(encoding="utf-8"), "sketch")
        self.assertEqual(len(self.agent.calls), 2,
                         "one call before the pause and one after: the agent continues, "
                         "it does not start over")
        values = graph.get_state({"configurable": {"thread_id": THREAD}}).values
        self.assertEqual(values["work_summary"], "request_gate: gate g1")
        self.assertTrue(values["check_results"], "the unit reached the checks after work")

    def test_approval_refuse_does_not_run_tool(self):
        graph = self.graph()
        # A refused write leaves the unit empty; g1 needs a real file in its surface.
        (self.unit_dir / "report.md").write_text("sketch\n", encoding="utf-8")
        self.pause(graph)
        self.proceed(graph, "refuse")
        self.assertEqual(runner.pending(graph, THREAD), [])
        self.assertEqual(self.runs, [], "after refuse the tool body must not run")
        self.assertFalse((self.unit_dir / "notes.md").exists())
        self.assertEqual(len(self.agent.calls), 2, "one call before the pause and one after")
        seen = [m for m in self.agent.calls[1] if isinstance(m, ToolMessage)]
        self.assertTrue(any("ask-path" in m.content for m in seen), seen)


if __name__ == "__main__":
    unittest.main()
