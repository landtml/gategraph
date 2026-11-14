"""Contract tests for core/graph/state.py and core/graph/contracts.py (brief C3).

Standard library and langgraph. The state and contract tests run with either
interpreter; the serializer round trip needs langgraph, so run this file with
.venv/bin/python.
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

# Strict msgpack must be set before langgraph is imported (see RESULTS Q14).
import os
os.environ["LANGGRAPH_STRICT_MSGPACK"] = "true"

import dataclasses
import sqlite3
import tempfile
import typing
import unittest

from core.kernel import types as T
from core.graph import state as S
from core.graph import contracts as C

HEX_A = "a" * 64
HEX_B = "b" * 64
HEX_C = "c" * 64

# Plan section 1.6, verbatim key list (18 keys).
UNIT_STATE_KEYS = {
    "project", "unit", "actor", "config_digest", "evidence", "status",
    "effective", "stale", "target_gate", "surface_digest", "check_results",
    "verdicts", "findings", "attempts", "fail_streak", "spend", "decisions",
    "work_summary",
}

# Plan section 1.5 node names (the nodes that are added with add_node).
EXPECTED_NODES = (
    "reconcile", "work", "check_fanout", "run_check", "judge_checks",
    "run_verifier", "judge_verdicts", "approve", "seal", "repair_route",
    "escalate", "finalize",
)

# Gate ids of the section 1.7 example. No node name may contain one.
EXAMPLE_GATE_IDS = ("scoped", "composed", "verified", "packaged")

MALFORMED = (
    None, 0, 5, -1, 3.5, float("nan"), True, "", "x", b"bytes", {}, {"seq": 9},
    {"seq": 5, "prev": "zz"}, [], [None], [object()], (1, 2), object(),
    {"a": {"b": [1, None]}},
)


def make_record(seq: int, prev: str = "") -> dict:
    """A realistic EvidenceRecord in its to_dict() form (plain JSON)."""
    producer = T.ProducerRecord(
        id="policy", kind=T.ProducerKind.CHECK, digest=HEX_B, exit=0,
        verdict=None, findings_digest=None)
    record = T.EvidenceRecord(
        v=1, seq=seq, kind=T.RecordKind.GATE, gate="verified",
        at="2026-11-13T00:00:00+00:00", surface_digest=HEX_A,
        producers=(producer,), config_digest=HEX_C,
        actor=T.Actor(kind=T.ActorKind.TOOL, id="gate", channel=None),
        prev=prev, digest=HEX_B, mac=None)
    return record.to_dict()


def full_state() -> dict:
    """A realistic 18-key UnitState built from the C1 types' to_dict() forms."""
    finding = T.Finding(rule="policy.secret", severity=T.Severity.FATAL,
                        path="a.md", line=3, message="example finding")
    check = T.CheckResult(id="policy", exit=1, status=T.CheckStatus.FATAL,
                          findings=(finding,), duration_s=0.5, digest=HEX_A)
    verdict = T.VerifierVerdict(id="reviewer", run=1, verdict="pass",
                                findings=(), digest=HEX_B,
                                artifact_sha256=HEX_C)
    decision = T.HumanDecision(request_id="r1", answer=T.HumanAnswer.APPROVE,
                               by="operator", channel="tty", note=None,
                               at="2026-11-13T00:00:00+00:00")
    spend = T.Spend(usd=0.0123, tokens=4567, model_calls=3, tool_calls=9,
                    first_started_at="2026-11-13T00:00:00+00:00",
                    iterations=2, elapsed_s=12.5)
    return {
        "project": "example", "unit": "report-01", "actor": "native",
        "config_digest": HEX_C,
        "evidence": [make_record(0), make_record(1, prev=HEX_B)],
        "status": "in_progress",
        "effective": ["scoped"], "stale": [], "target_gate": "verified",
        "surface_digest": HEX_A,
        "check_results": [check.to_dict()],
        "verdicts": [verdict.to_dict()],
        "findings": [finding.to_dict()],
        "attempts": {"verified": 2, "composed": 0},
        "fail_streak": 1,
        "spend": spend.to_dict(),
        "decisions": [decision.to_dict()],
        "work_summary": "unicode æøå ✓",
    }


class TestUnitStateSchema(unittest.TestCase):
    def test_unit_state_has_exactly_the_section_1_6_keys(self):
        self.assertEqual(set(S.UnitState.__annotations__), UNIT_STATE_KEYS)

    def test_full_state_fixture_has_exactly_the_section_1_6_keys(self):
        self.assertEqual(set(full_state()), UNIT_STATE_KEYS)

    def test_search_state_is_a_typed_dict(self):
        self.assertTrue(typing.is_typeddict(S.SearchState))
        for key in ("repairs", "reproposals", "revisions", "fail_streak",
                    "spend", "stop_reason"):
            self.assertIn(key, S.SearchState.__annotations__)

    def test_evidence_in_state_carries_surface_digest_and_no_surface_map(self):
        record = make_record(0)
        self.assertIn("surface_digest", record)
        self.assertNotIn("surface", record)
        for item in full_state()["evidence"]:
            self.assertNotIn("surface", item)
            self.assertEqual(item["surface_digest"], HEX_A)

    def test_evidence_record_to_dict_has_no_surface_key(self):
        rec = T.EvidenceRecord(
            v=1, seq=0, kind=T.RecordKind.GATE, gate="g",
            at="2026-11-13T00:00:00+00:00", surface_digest=HEX_A, producers=(),
            config_digest=HEX_C,
            actor=T.Actor(kind=T.ActorKind.HUMAN, id="op", channel="tty"),
            prev="", digest=HEX_B)
        self.assertNotIn("surface", rec.to_dict())
        self.assertIn("surface_digest", rec.to_dict())


class TestNodeNames(unittest.TestCase):
    def test_node_names_is_a_frozen_tuple_of_the_section_1_5_nodes(self):
        self.assertIsInstance(S.NODE_NAMES, tuple)
        self.assertEqual(S.NODE_NAMES, EXPECTED_NODES)

    def test_node_names_are_unique(self):
        self.assertEqual(len(set(S.NODE_NAMES)), len(S.NODE_NAMES))

    def test_no_node_name_contains_a_gate_name(self):
        for name in S.NODE_NAMES:
            for gate in EXAMPLE_GATE_IDS:
                self.assertNotIn(gate, name, f"node {name!r} contains gate {gate!r}")

    def test_routing_names_are_not_nodes(self):
        self.assertTrue(set(S.ROUTING_NAMES).isdisjoint(S.NODE_NAMES))
        self.assertIn("dispatch", S.ROUTING_NAMES)


class TestReducers(unittest.TestCase):
    def test_append_evidence_appends(self):
        first, second = make_record(0), make_record(1, prev=HEX_B)
        self.assertEqual(S.append_evidence([first], [second]), [first, second])

    def test_append_evidence_does_not_mutate_its_inputs(self):
        old = [make_record(0)]
        S.append_evidence(old, [make_record(1)])
        self.assertEqual(len(old), 1)

    def test_append_evidence_accepts_wrong_seq_and_wrong_prev(self):
        old = [make_record(0)]
        bad = make_record(5, prev="not-the-digest")
        result = S.append_evidence(old, [bad])
        self.assertEqual(len(result), 2)
        self.assertEqual(result[1]["seq"], 5)

    def test_append_evidence_never_raises_on_any_input(self):
        for old in MALFORMED:
            for new in MALFORMED:
                with self.subTest(old=repr(old)[:40], new=repr(new)[:40]):
                    result = S.append_evidence(old, new)
                    self.assertIsInstance(result, list)

    def test_append_evidence_wraps_a_single_record(self):
        rec = make_record(0)
        self.assertEqual(S.append_evidence([], rec), [rec])

    def test_append_evidence_none_old_is_empty(self):
        rec = make_record(0)
        self.assertEqual(S.append_evidence(None, [rec]), [rec])

    def test_collect_appends(self):
        self.assertEqual(S.collect([1], [2, 3]), [1, 2, 3])

    def test_collect_none_update_clears(self):
        self.assertEqual(S.collect([1, 2], None), [])
        self.assertEqual(S.collect(None, None), [])

    def test_collect_none_old_and_wrapped_new(self):
        self.assertEqual(S.collect(None, [4]), [4])
        self.assertEqual(S.collect([], {"id": "x"}), [{"id": "x"}])

    def test_append_records_appends_and_is_total(self):
        self.assertEqual(S.append_records([1], [2]), [1, 2])
        for old in MALFORMED:
            for new in MALFORMED:
                self.assertIsInstance(S.append_records(old, new), list)

    def test_every_reducer_is_total_over_malformed_input_Q12(self):
        # RESULTS Q12: a reducer that raises poisons the thread. No reducer
        # in state.py may raise, on any input a node can return.
        reducers = [S.append_evidence, S.collect, S.append_records]
        for reducer in reducers:
            for old in MALFORMED:
                for new in MALFORMED:
                    with self.subTest(reducer=reducer.__name__,
                                      old=repr(old)[:40], new=repr(new)[:40]):
                        result = reducer(old, new)
                        self.assertIsInstance(result, list)

    def test_reducers_are_the_ones_annotated_on_unit_state(self):
        hints = typing.get_type_hints(S.UnitState, include_extras=True)
        reducer_of = {}
        for key, hint in hints.items():
            for meta in getattr(hint, "__metadata__", ()):
                if callable(meta):
                    reducer_of[key] = meta
        self.assertIs(reducer_of["evidence"], S.append_evidence)
        self.assertIs(reducer_of["check_results"], S.collect)
        self.assertIs(reducer_of["verdicts"], S.collect)
        self.assertIs(reducer_of["decisions"], S.append_records)


class TestWriteTable(unittest.TestCase):
    def test_every_write_table_key_is_a_node(self):
        self.assertTrue(set(C.WRITES).issubset(S.NODE_NAMES))

    def test_every_node_has_a_write_entry_and_a_protocol(self):
        self.assertEqual(set(C.WRITES), set(S.NODE_NAMES))
        self.assertEqual(set(C.PROTOCOLS), set(S.NODE_NAMES))

    def test_every_written_key_is_a_unit_state_key(self):
        for node, keys in C.WRITES.items():
            self.assertIsInstance(keys, frozenset, node)
            self.assertTrue(keys.issubset(UNIT_STATE_KEYS),
                            f"{node} writes non-state keys {set(keys) - UNIT_STATE_KEYS}")

    def test_protocols_are_typing_protocols(self):
        for node, proto in C.PROTOCOLS.items():
            self.assertTrue(getattr(proto, "_is_protocol", False), node)

    def test_evidence_is_written_only_by_seal_and_escalate(self):
        # Plan 1.5 rule 3: evidence is appended only in seal. Escalation records
        # its decision as an evidence record of kind escalation (1.5 table).
        writers = {node for node, keys in C.WRITES.items() if "evidence" in keys}
        self.assertEqual(writers, {"seal", "escalate"})

    def test_run_check_writes_only_check_results(self):
        self.assertEqual(C.WRITES["run_check"], frozenset({"check_results"}))


class TestContracts(unittest.TestCase):
    def test_step_literals(self):
        self.assertEqual(typing.get_args(C.STEP),
                         ("work", "check", "escalate", "finalize", "end"))
        self.assertEqual(C.STEPS, typing.get_args(C.STEP))

    def test_work_result_fields(self):
        names = [f.name for f in dataclasses.fields(C.WorkResult)]
        self.assertEqual(names, ["summary", "spend", "finished", "gate"])  # amendment 5

    def test_work_actor_protocol_and_a_conforming_fake(self):
        class FakeActor:
            def run(self, goal, findings, context):
                return C.WorkResult(summary="done", spend=T.Spend())

        self.assertIsInstance(FakeActor(), C.WorkActor)

        class NotAnActor:
            def go(self):
                return None

        self.assertNotIsInstance(NotAnActor(), C.WorkActor)
        result = FakeActor().run("goal", [], {})
        self.assertEqual(result.summary, "done")


class TestStrictSerializerRoundTrip(unittest.TestCase):
    def serde(self):
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
        return JsonPlusSerializer(pickle_fallback=False, allowed_msgpack_modules=None)

    def test_full_state_round_trips_directly(self):
        want = full_state()
        direct = self.serde()
        got = direct.loads_typed(direct.dumps_typed(want))
        self.assertEqual(got, want)

    def test_full_state_round_trips_through_a_sqlite_checkpoint(self):
        from langgraph.checkpoint.sqlite import SqliteSaver
        from langgraph.graph import END, START, StateGraph

        want = full_state()
        graph = StateGraph(S.UnitState)
        graph.add_node("write", lambda state: want)
        graph.add_edge(START, "write")
        graph.add_edge("write", END)
        path = os.path.join(tempfile.mkdtemp(prefix="c3-"), "s.db")
        conn = sqlite3.connect(path, check_same_thread=False)
        app = graph.compile(checkpointer=SqliteSaver(conn, serde=self.serde()))
        cfg = {"configurable": {"thread_id": "t"}}
        app.invoke({}, cfg, durability="sync")
        self.assertEqual(app.get_state(cfg).values, want)

        fresh = SqliteSaver(sqlite3.connect(path, check_same_thread=False),
                            serde=self.serde())
        stored = fresh.get_tuple(cfg).checkpoint["channel_values"]
        self.assertEqual({k: v for k, v in stored.items() if k in want}, want)

    def test_search_state_round_trips_directly(self):
        want = {
            "project": "example", "brief_digest": HEX_A, "status": "in_progress",
            "stop_reason": None, "variants": [{"slot": 0, "ok": True}],
            "proposals": [{"id": "p1", "score": 0.5}], "repairs": 1,
            "reproposals": 0, "revisions": 2, "fail_streak": 0,
            "spend": T.Spend(usd=0.5, iterations=3, elapsed_s=2.0).to_dict(),
            "decisions": [],
        }
        direct = self.serde()
        self.assertEqual(direct.loads_typed(direct.dumps_typed(want)), want)


if __name__ == "__main__":
    unittest.main(verbosity=2)
