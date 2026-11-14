"""Human-in-the-loop nodes: approve and escalate (plan sections 1.6, 1.7, 1.12; B02, B38).

Each test builds a one-node graph with InMemorySaver and drives it with a real
interrupt and Command(resume=...). The last test inspects the syntax tree of
both node modules: one interrupt call per node, and no side effect before it.

Runs on .venv/bin/python (graph layer).
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import ast
import tempfile
import unittest
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from core.graph.nodes import approve as approve_mod
from core.graph.nodes import escalate as escalate_mod
from core.graph.state import UnitState
from core.kernel.ledger import read_ledger
from core.kernel.types import (Budget, HumanAnswer, HumanDecision, HumanKind, HumanRequest,
                               Surface)

HEX_A = "a" * 64
CONFIG = "c" * 64
EMPTY_SURFACE = Surface(files={}).digest()
REPO = _bootstrap.REPO
NODE_FILES = (REPO / "core/graph/nodes/approve.py", REPO / "core/graph/nodes/escalate.py")


def _graph(node_fn, name):
    """A one-node graph. The node sees the LangGraph config's ``configurable`` mapping."""
    g = StateGraph(UnitState)
    g.add_node(name, lambda state, config: node_fn(state, config["configurable"]))
    g.add_edge(START, name)
    g.add_edge(name, END)
    return g.compile(checkpointer=InMemorySaver())


def _state(**over):
    base = {
        "project": "p", "unit": "u1", "actor": "native", "config_digest": CONFIG,
        "evidence": [], "status": "escalated", "effective": [], "stale": [],
        "target_gate": "G1", "surface_digest": HEX_A,
        "check_results": [], "verdicts": [], "findings": [],
        "attempts": {"G1": 3}, "fail_streak": 3, "spend": {}, "decisions": [],
    }
    base.update(over)
    return base


def _cfg(root, thread, **extra):
    return {"configurable": {"thread_id": thread, "root": root, **extra}}


def _pending(result):
    """The typed request of the one interrupt the graph raised."""
    interrupts = result["__interrupt__"]
    assert len(interrupts) == 1, interrupts
    return HumanRequest.from_dict(interrupts[0].value)


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class ApproveNodeTests(_TmpCase):
    def _app(self):
        return _graph(approve_mod.approve, "approve")

    def test_approve_pauses_with_typed_request_and_proceeds_with_approve(self):
        app = self._app()
        cfg = _cfg(self.root, "t-approve", channel="tty")
        first = app.invoke(_state(status="in_progress"), cfg)
        req = _pending(first)
        self.assertEqual(req.kind, HumanKind.APPROVAL)
        self.assertEqual(req.gate, "G1")
        self.assertEqual(req.payload["surface_digest"], HEX_A)
        self.assertIsNone(req.token_digest)

        final = app.invoke(Command(resume={"answer": "approve", "by": "ana",
                                           "channel": "tty", "note": None}), cfg)
        self.assertNotIn("__interrupt__", final)
        (dec,) = final["decisions"]
        self.assertEqual(dec["answer"], "approve")
        self.assertEqual(dec["by"], "ana")
        self.assertEqual(dec["request_id"], req.id)
        HumanDecision.from_dict(dec)  # the stored record is a typed decision

    def test_approve_proceeds_with_reject_and_note(self):
        app = self._app()
        cfg = _cfg(self.root, "t-reject", channel="tty")
        app.invoke(_state(status="in_progress"), cfg)
        final = app.invoke(Command(resume={"answer": "reject", "by": "ana",
                                           "channel": "tty", "note": "tests missing"}), cfg)
        (dec,) = final["decisions"]
        self.assertEqual(dec["answer"], "reject")
        self.assertEqual(dec["note"], "tests missing")

    def test_approve_refuses_an_answer_outside_the_schema(self):
        # A refused proceed stays stored on its thread (LangGraph keeps the pending
        # write), so each bad answer gets its own thread.
        bad_answers = [
            {"answer": "retry", "by": "ana", "channel": "tty", "note": None},
            {"answer": "approve", "by": "", "channel": "tty", "note": None},
            {"answer": "reject", "by": "ana", "channel": "tty", "note": None},
            {"answer": "approve", "by": "ana", "channel": "tty", "note": None, "extra": 1},
            "approve",
        ]
        for index, bad in enumerate(bad_answers):
            with self.subTest(bad=bad):
                app = self._app()
                cfg = _cfg(self.root, f"t-refuse-{index}", channel="tty")
                app.invoke(_state(status="in_progress"), cfg)
                with self.assertRaises(approve_mod.HumanAnswerRefused):
                    app.invoke(Command(resume=bad), cfg)
                self.assertEqual(app.get_state(cfg).next, ("approve",))
                self.assertFalse(app.get_state(cfg).values.get("decisions"))


class EscalateNodeTests(_TmpCase):
    def _app(self):
        return _graph(escalate_mod.escalate, "escalate")

    def _proceed(self, app, cfg, answer, **over):
        body = {"answer": answer, "by": "ana", "channel": "tty", "note": "why"}
        body.update(over)
        return app.invoke(Command(resume=body), cfg)

    def test_escalate_pauses_with_typed_request_listing_allowed_answers(self):
        app = self._app()
        cfg = _cfg(self.root, "t-esc-req", allow_waivers=True, channel="tty")
        req = _pending(app.invoke(_state(), cfg))
        self.assertEqual(req.kind, HumanKind.ESCALATION)
        self.assertEqual(req.gate, "G1")
        self.assertEqual(req.payload["fail_streak"], 3)
        self.assertEqual(sorted(req.payload["answers"]), ["abandon", "retry", "waive"])

    def test_B02_escalated_unit_leaves_only_through_retry(self):
        # Any answer other than retry leaves the escalated status as a terminal one.
        for answer in ("abandon", "waive"):
            with self.subTest(answer=answer):
                app = self._app()
                cfg = _cfg(self.root, f"t-b02-{answer}", allow_waivers=True, channel="tty")
                app.invoke(_state(), cfg)
                final = self._proceed(app, cfg, answer)
                self.assertNotIn(final["status"], ("in_progress", "paused", "escalated"))
                self.assertIn(final["status"], ("abandoned", "complete"))
        # retry is the only answer that reopens the unit, and it is recorded.
        app = self._app()
        cfg = _cfg(self.root, "t-b02-retry", channel="tty")
        app.invoke(_state(), cfg)
        final = self._proceed(app, cfg, "retry")
        self.assertEqual(final["status"], "in_progress")
        self.assertEqual(final["fail_streak"], 0)
        self.assertEqual(final["attempts"], {"G1": 0})
        (dec,) = final["decisions"]
        self.assertEqual(dec["answer"], "retry")
        (rec,) = final["evidence"]
        self.assertEqual(rec["kind"], "escalation")
        self.assertEqual(rec["seq"], 0)

    def test_retry_resets_only_the_escalated_gate_count(self):
        app = self._app()
        cfg = _cfg(self.root, "t-reset", channel="tty")
        app.invoke(_state(attempts={"G1": 3, "G2": 2}), cfg)
        final = self._proceed(app, cfg, "retry")
        self.assertEqual(final["attempts"], {"G1": 0, "G2": 2})

    def test_waive_is_refused_unless_the_adapter_allows_waivers(self):
        app = self._app()
        cfg = _cfg(self.root, "t-nowaive", allow_waivers=False, channel="tty")
        req = _pending(app.invoke(_state(), cfg))
        self.assertNotIn("waive", req.payload["answers"])
        with self.assertRaises(escalate_mod.HumanAnswerRefused):
            self._proceed(app, cfg, "waive")

    def test_B38_every_escalation_writes_a_ledger_row(self):
        for answer in ("retry", "abandon"):
            with self.subTest(answer=answer):
                app = self._app()
                cfg = _cfg(self.root, f"t-b38-{answer}", channel="tty")
                app.invoke(_state(), cfg)
                self._proceed(app, cfg, answer)
        app = self._app()
        cfg = _cfg(self.root, "t-b38-waive", allow_waivers=True, channel="tty")
        app.invoke(_state(), cfg)
        self._proceed(app, cfg, "waive")
        rows = [r for r in read_ledger(self.root) if r.get("kind") == "escalation"]
        self.assertEqual(len(rows), 3)
        self.assertEqual({r["answer"] for r in rows}, {"retry", "abandon", "waive"})
        self.assertTrue(all(r["unit"] == "u1" and r["gate"] == "G1" for r in rows))

    def test_each_escalation_writes_exactly_one_ledger_row(self):
        app = self._app()
        cfg = _cfg(self.root, "t-once", channel="tty")
        app.invoke(_state(), cfg)
        self._proceed(app, cfg, "retry")
        rows = [r for r in read_ledger(self.root) if r.get("kind") == "escalation"]
        self.assertEqual(len(rows), 1)

    def test_waiver_is_recorded_as_a_waiver_record(self):
        app = self._app()
        cfg = _cfg(self.root, "t-waiver-rec", allow_waivers=True, channel="tty")
        app.invoke(_state(), cfg)
        final = self._proceed(app, cfg, "waive")
        (rec,) = final["evidence"]
        self.assertEqual(rec["kind"], "waiver")

    def test_escalate_before_any_attempt_interrupts_and_records(self):
        app = self._app()
        cfg = _cfg(self.root, "t-pre-attempt", channel="tty",
                   budget=Budget(max_iterations=2))
        state = _state(target_gate=None, surface_digest=None, attempts={}, fail_streak=0,
                       spend={"iterations": 5})
        req = _pending(app.invoke(state, cfg))
        self.assertEqual(req.kind, HumanKind.ESCALATION)
        self.assertIsNone(req.gate)
        self.assertIn("budget", req.payload["reason"])
        final = self._proceed(app, cfg, "abandon")
        (rec,) = final["evidence"]
        self.assertEqual(rec["kind"], "escalation")
        self.assertIsNone(rec["gate"])
        self.assertEqual(rec["surface_digest"], EMPTY_SURFACE)
        rows = [r for r in read_ledger(self.root) if r.get("kind") == "escalation"]
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["gate"])

    def test_escalate_reason_names_integrity_finding(self):
        app = self._app()
        cfg = _cfg(self.root, "t-integrity", channel="tty")
        finding = {"rule": "integrity.chain_broken", "severity": "fatal",
                   "path": None, "line": None, "message": "chain broken"}
        state = _state(target_gate=None, surface_digest=None, attempts={}, fail_streak=0,
                       findings=[finding])
        req = _pending(app.invoke(state, cfg))
        self.assertIsNone(req.gate)
        self.assertIn("integrity.chain_broken", req.payload["reason"])

    def test_retry_without_gate_resets_streak_and_keeps_attempts(self):
        app = self._app()
        cfg = _cfg(self.root, "t-retry-nogate", channel="tty")
        app.invoke(_state(target_gate=None, surface_digest=None, attempts={"G2": 2},
                          fail_streak=3), cfg)
        final = self._proceed(app, cfg, "retry")
        self.assertEqual(final["status"], "in_progress")
        self.assertEqual(final["fail_streak"], 0)
        self.assertEqual(final["attempts"], {"G2": 2})

    def test_waive_without_gate_is_refused(self):
        app = self._app()
        cfg = _cfg(self.root, "t-waive-nogate", allow_waivers=True, channel="tty")
        req = _pending(app.invoke(_state(target_gate=None, surface_digest=None, attempts={},
                                         fail_streak=0), cfg))
        self.assertNotIn("waive", req.payload["answers"])
        with self.assertRaises(escalate_mod.HumanAnswerRefused):
            self._proceed(app, cfg, "waive")


class SyntaxTreeTests(unittest.TestCase):
    """One interrupt per node, and nothing with a side effect before it (section 1.12)."""

    PURE = {"len", "dict", "list", "str", "int", "bool", "isinstance", "sorted", "tuple",
            "set", "min", "max", "sum", "getattr", "HumanRequest", "HumanKind",
            "HumanDecision", "HumanAnswer", "HUMAN_ANSWERS", "canonical_json",
            "request_id", "_utc_now", "is_digest", "get", "to_dict", "copy", "items",
            "keys", "values", "allowed_answers", "_reasons", "join"}

    def _function(self, path, name):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        self.fail(f"{path.name} has no function {name}")

    def _call_name(self, call):
        func = call.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
        return None

    def _check_node(self, path, name):
        fn = self._function(path, name)
        interrupts = [c for c in ast.walk(fn)
                      if isinstance(c, ast.Call) and self._call_name(c) == "interrupt"]
        self.assertEqual(len(interrupts), 1, f"{name} must contain exactly one interrupt call")
        # Find the body statement that holds the interrupt call.
        cut = None
        for index, stmt in enumerate(fn.body):
            if any(c is interrupts[0] for c in ast.walk(stmt)):
                cut = index
                break
        self.assertIsNotNone(cut)
        for stmt in fn.body[:cut + 1]:
            for call in ast.walk(stmt):
                if not isinstance(call, ast.Call) or call is interrupts[0]:
                    continue
                cname = self._call_name(call)
                ok = cname in self.PURE or (cname or "").endswith(("Error", "Refused"))
                self.assertTrue(ok, f"{name}: call to {cname!r} before interrupt "
                                    f"(line {call.lineno}) may have a side effect")

    def test_approve_has_one_interrupt_and_no_side_effect_before_it(self):
        self._check_node(NODE_FILES[0], "approve")

    def test_escalate_has_one_interrupt_and_no_side_effect_before_it(self):
        self._check_node(NODE_FILES[1], "escalate")


if __name__ == "__main__":
    unittest.main(verbosity=2)
