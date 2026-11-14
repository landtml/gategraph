"""G6: the work node, native and external (plan sections 1.5, 1.11, 1.12, 1.16).

Runs on the pinned venv: the external path needs LangGraph's interrupt().
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import json
import unittest
from types import SimpleNamespace

from langgraph.checkpoint.memory import MemorySaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Interrupt

from core.graph.nodes import work as work_mod
from core.graph.contracts import WRITES, WorkActor, WorkResult
from core.graph.state import UnitState
from core.graph.nodes.work import lookup_work_actor, register_work_actor, work
from core.kernel.types import GateSpec, Spend


class ContractError(Exception):
    """Matched by class name along the MRO (core/kernel/faults.py)."""


class RateLimited(Exception):
    """Infrastructure, never a strike."""


class FakeActor:
    """Records its inputs and returns a scripted outcome."""

    def __init__(self, outcome=None):
        self.outcome = outcome
        self.calls = []
        self.messages = ["SECRET-TRANSCRIPT: the full agent conversation"]

    def run(self, goal, findings, context):
        self.calls.append((goal, list(findings), dict(context)))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


CFG = SimpleNamespace(gates=(
    GateSpec(id="composed"),
    GateSpec(id="reviewed", needs=("composed",)),
))

FINDING = {"rule": "style.heading", "severity": "fatal", "path": "a.md",
           "line": 3, "message": "heading level skipped", "remedy": None}


def _external_state(**extra):
    state = {"project": "p", "unit": "u1", "actor": "external",
             "findings": [FINDING], "effective": [], "attempts": {},
             "spend": {}, "target_gate": None}
    state.update(extra)
    return state


def _graph(cfg):
    g = StateGraph(UnitState)
    g.add_node("work", lambda state: work(state, cfg))
    g.add_edge(START, "work")
    g.add_edge("work", END)
    return g.compile(checkpointer=MemorySaver())


class TestTargetAndRegistry(unittest.TestCase):
    def test_registry_returns_registered_actor(self):
        actor = FakeActor(outcome=None)
        register_work_actor("test-g6-registry", actor)
        self.assertIs(lookup_work_actor("test-g6-registry"), actor)

    def test_registry_rejects_non_actor(self):
        with self.assertRaises(TypeError):
            register_work_actor("test-g6-bad", object())

    def test_unknown_actor_name_is_a_lookup_error(self):
        with self.assertRaises(LookupError):
            lookup_work_actor("test-g6-missing")

    def test_work_actor_protocol_is_checkable(self):
        self.assertTrue(isinstance(FakeActor(), WorkActor))

    def test_target_is_first_gate_whose_needs_are_effective(self):
        self.assertEqual(work_mod._target(_external_state(), CFG), "composed")
        self.assertEqual(
            work_mod._target(_external_state(effective=["composed"]), CFG), "reviewed")


class TestExternalActor(unittest.TestCase):
    def test_pauses_with_work_request_naming_gate_and_findings(self):
        app = _graph(CFG)
        actor = FakeActor(outcome=WorkResult(summary="x", spend=Spend()))
        register_work_actor("test-g6-ext-quiet", actor)
        config = {"configurable": {"thread_id": "g6-pause"}}
        first = app.invoke(_external_state(), config)
        self.assertIn("__interrupt__", first)
        request = first["__interrupt__"][0].value
        self.assertEqual(request["kind"], "work")
        self.assertEqual(request["gate"], "composed")
        self.assertEqual(request["payload"]["gate"], "composed")
        self.assertEqual(request["payload"]["findings"], [FINDING])
        self.assertEqual(actor.calls, [])

    def test_proceeds_on_attempt_and_sets_target_gate(self):
        app = _graph(CFG)
        config = {"configurable": {"thread_id": "g6-attempt"}}
        app.invoke(_external_state(), config)
        final = app.invoke(Command(resume="attempt"), config)
        self.assertEqual(final.get("target_gate"), "composed")
        self.assertNotIn("work_summary", final)

    def test_other_answer_does_not_attempt(self):
        app = _graph(CFG)
        config = {"configurable": {"thread_id": "g6-other"}}
        app.invoke(_external_state(), config)
        final = app.invoke(Command(resume="wait"), config)
        self.assertIsNone(final.get("target_gate"))


class TestNativeActor(unittest.TestCase):
    def setUp(self):
        self.prior = {"usd": 1.0, "tokens": 50, "model_calls": 1, "tool_calls": 0,
                      "first_started_at": "2026-11-01T00:00:00+00:00",
                      "iterations": 2, "elapsed_s": 10.0}

    def test_stores_only_summary_and_folded_spend(self):
        actor = FakeActor(outcome=WorkResult(
            summary="composed the section",
            spend=Spend(usd=0.25, tokens=100, model_calls=2, tool_calls=3),
            finished=True, gate="composed"))
        register_work_actor("test-g6-native-ok", actor)
        state = _external_state(actor="native", spend=self.prior)
        update = work(state, dict(CFG_NATIVE, work_actor="test-g6-native-ok"))
        self.assertEqual(set(update), {"work_summary", "spend", "target_gate"})
        self.assertEqual(update["target_gate"], "composed")
        self.assertEqual(update["work_summary"], "composed the section")
        self.assertNotIn("SECRET-TRANSCRIPT", json.dumps(update, default=str))
        self.assertAlmostEqual(update["spend"]["usd"], 1.25)
        self.assertEqual(update["spend"]["tokens"], 150)
        self.assertEqual(update["spend"]["model_calls"], 3)
        self.assertEqual(update["spend"]["tool_calls"], 3)
        self.assertEqual(update["spend"]["iterations"], 2)
        self.assertEqual(update["spend"]["first_started_at"], self.prior["first_started_at"])
        self.assertTrue(set(update) <= WRITES["work"])

    def test_actor_receives_goal_findings_and_context_without_messages(self):
        actor = FakeActor(outcome=WorkResult(summary="ok", spend=Spend()))
        register_work_actor("test-g6-native-args", actor)
        work(_external_state(actor="native"), dict(CFG_NATIVE, work_actor="test-g6-native-args"))
        goal, findings, context = actor.calls[0]
        self.assertIn("composed", goal)
        self.assertEqual(findings, [FINDING])
        self.assertEqual(context["gate"], "composed")
        self.assertNotIn("messages", context)

    def test_variant_fault_becomes_findings(self):
        actor = FakeActor(outcome=ContractError("output missed the schema"))
        register_work_actor("test-g6-native-variant", actor)
        update = work(_external_state(actor="native"),
                      dict(CFG_NATIVE, work_actor="test-g6-native-variant"))
        self.assertNotIn("work_summary", update)
        self.assertNotIn("spend", update)
        self.assertEqual(len(update["findings"]), 1)
        finding = update["findings"][0]
        self.assertEqual(finding["severity"], "fatal")
        self.assertIn("output missed the schema", finding["message"])

    def test_infrastructure_fault_propagates(self):
        for exc in (RuntimeError("spawn failed"), RateLimited("slow down")):
            actor = FakeActor(outcome=exc)
            name = f"test-g6-native-infra-{type(exc).__name__}"
            register_work_actor(name, actor)
            with self.subTest(fault=type(exc).__name__):
                with self.assertRaises(type(exc)):
                    work(_external_state(actor="native"), dict(CFG_NATIVE, work_actor=name))

    def test_missing_registration_raises(self):
        with self.assertRaises(LookupError):
            work(_external_state(actor="native"),
                 dict(CFG_NATIVE, work_actor="test-g6-never-registered"))

    def test_native_finished_sets_target_gate(self):
        actor = FakeActor(outcome=WorkResult(summary="done", spend=Spend(),
                                             finished=True, gate="reviewed"))
        register_work_actor("test-g6-native-finished", actor)
        update = work(_external_state(actor="native", effective=["composed"]),
                      dict(CFG_NATIVE, work_actor="test-g6-native-finished"))
        self.assertEqual(update["target_gate"], "reviewed")
        self.assertEqual(update["work_summary"], "done")
        self.assertNotIn("findings", update)
        self.assertTrue(set(update) <= WRITES["work"])

    def test_native_finished_without_gate_targets_first_ready(self):
        actor = FakeActor(outcome=WorkResult(summary="done", spend=Spend(), finished=True))
        register_work_actor("test-g6-native-first", actor)
        cfg = dict(CFG_NATIVE, work_actor="test-g6-native-first")
        none_ready = work(_external_state(actor="native", effective=[]), cfg)
        self.assertEqual(none_ready["target_gate"], "composed")
        self.assertTrue(set(none_ready) <= WRITES["work"])
        composed = work(_external_state(actor="native", effective=["composed"]), cfg)
        self.assertEqual(composed["target_gate"], "reviewed")
        self.assertTrue(set(composed) <= WRITES["work"])

    def test_native_unfinished_leaves_target_and_returns_findings(self):
        actor = FakeActor(outcome=WorkResult(summary="half the section",
                                             spend=Spend(usd=0.1), finished=False))
        register_work_actor("test-g6-native-unfinished", actor)
        update = work(_external_state(actor="native"),
                      dict(CFG_NATIVE, work_actor="test-g6-native-unfinished"))
        self.assertNotIn("target_gate", update)
        self.assertEqual(set(update), {"work_summary", "spend", "findings"})
        self.assertEqual(len(update["findings"]), 1)
        finding = update["findings"][0]
        self.assertEqual(finding["rule"], "work.unfinished")
        self.assertEqual(finding["severity"], "fatal")
        self.assertIn("half the section", finding["message"])
        self.assertTrue(set(update) <= WRITES["work"])

    def test_native_unknown_gate_is_a_finding(self):
        actor = FakeActor(outcome=WorkResult(summary="x", spend=Spend(),
                                             finished=True, gate="no-such-gate"))
        register_work_actor("test-g6-native-unknown", actor)
        update = work(_external_state(actor="native"),
                      dict(CFG_NATIVE, work_actor="test-g6-native-unknown"))
        self.assertNotIn("target_gate", update)
        self.assertEqual(set(update), {"work_summary", "spend", "findings"})
        self.assertEqual(len(update["findings"]), 1)
        finding = update["findings"][0]
        self.assertEqual(finding["rule"], "work.unknown_gate")
        self.assertEqual(finding["severity"], "fatal")
        self.assertIn("no-such-gate", finding["message"])
        self.assertTrue(set(update) <= WRITES["work"])


CFG_NATIVE = {"gates": CFG.gates}


class PausingActor:
    """A native actor whose agent asks a human: its run raises GraphInterrupt carrying the
    interrupt, as LangGraph does when a nested graph pauses."""

    def __init__(self):
        self.calls = 0

    def run(self, goal, findings, context):
        self.calls += 1
        raise GraphInterrupt((Interrupt(value={"kind": "ask", "tool": "publish_note"},
                                        id="nested-1"),))


class TestNativeInterruptBubblesUp(unittest.TestCase):
    """An interrupt raised by a native actor (a nested agent's tool needs a human) pauses the
    unit. It is not a variant fault and not a finding; the node lets it through."""

    def test_graph_interrupt_from_the_actor_propagates_out_of_the_node(self):
        actor = PausingActor()
        register_work_actor("test-g6-bubble", actor)
        state = _external_state(actor="native", findings=[])
        with self.assertRaises(GraphInterrupt):
            work(state, dict(CFG_NATIVE, work_actor="test-g6-bubble"))
        self.assertEqual(actor.calls, 1)

    def test_graph_interrupt_pauses_the_graph_without_a_finding(self):
        register_work_actor("test-g6-bubble-graph", PausingActor())
        cfg = dict(CFG_NATIVE, work_actor="test-g6-bubble-graph")
        app = _graph(cfg)
        config = {"configurable": {"thread_id": "g6-bubble"}}
        first = app.invoke(_external_state(actor="native", findings=[]), config)
        self.assertIn("__interrupt__", first)
        self.assertEqual(first.get("findings"), [])
        self.assertNotIn("work_summary", first)


if __name__ == "__main__":
    unittest.main()
