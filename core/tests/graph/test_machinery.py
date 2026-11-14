"""G7: the four machinery scenarios of eval/corpus/machinery.yaml, run against the unit graph.

Scenario names and expected verdicts: tests/golden/expected/_machinery.json (all pass: true for
the legacy engine). Run standalone: ``.venv/bin/python core/tests/graph/test_machinery.py``.
The graph is driven by the external actor in test_unit_graph.py; no model is called except
the FakeModel verifier that test_unit_graph.Project supplies.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import tempfile
import unittest

from core.kernel.types import GateSpec, RepairSpec
from test_unit_graph import (THREAD, Project, check, drive, pending, run, ATTEMPT)
from langgraph.types import Command


class MachineryScenarios(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._n = 0

    def project(self, gates, checks):
        self._n += 1
        return Project(f"{self._tmp.name}/case{self._n}", gates, checks)

    def test_out_of_order_refused(self):
        # The gate g2 needs g1. The external actor cannot name a gate; an injected target
        # for g2 must be refused at seal, so no record for g2 reaches the chain.
        gates = [GateSpec(id="g1", checks=("c1",)),
                 GateSpec(id="g2", needs=("g1",), checks=("c2",))]
        project = self.project(gates, [check("c1", "true"), check("c2", "true")])
        graph = project.graph()
        project.start(graph)
        self.assertEqual(pending(graph)[0]["gate"], "g1")
        graph.update_state({"configurable": {"thread_id": THREAD}}, {"target_gate": "g2"})
        run(graph, project, Command(resume=ATTEMPT))
        self.assertEqual(project.evidence_gates(graph), [])
        self.assertEqual(project.values(graph)["effective"], [])
        refusal = pending(graph)[0]
        self.assertEqual(refusal["gate"], "g1")
        self.assertIn("seal.needs_not_effective",
                      [f["rule"] for f in refusal["payload"]["findings"]])

    def test_staleness_reopens(self):
        gates = [GateSpec(id="g1", checks=("c1",)), GateSpec(id="g2", needs=("g1",), checks=("c2",))]
        project = self.project(gates, [check("c1", "true"), check("c2", "true")])
        graph = project.graph()
        project.start(graph)
        run(graph, project, Command(resume=ATTEMPT))          # seal g1
        self.assertEqual(project.evidence_gates(graph), ["g1"])
        self.assertEqual(project.values(graph)["effective"], ["g1"])
        project.artifact.write_text("sketch two\n", encoding="utf-8")   # edit inside the surface
        run(graph, project, Command(resume=ATTEMPT))          # g2 attempt meets the edit
        values = project.values(graph)
        self.assertIn("g1", values["stale"])
        self.assertNotIn("g1", values["effective"])
        self.assertEqual(project.evidence_gates(graph), ["g1"])
        self.assertEqual(pending(graph)[0]["gate"], "g1")     # the reopened gate is the target

    def test_reset_invalidates_later(self):
        # Re-earning g1 with the files unchanged must drop g2, as the legacy manifest did.
        gates = [GateSpec(id="g1", checks=("c1",)), GateSpec(id="g2", needs=("g1",), checks=("c2",)),
                 GateSpec(id="g3", needs=("g2",), checks=("c3",))]
        checks = [check("c1", "true"), check("c2", "true"), check("c3", "true")]
        project = self.project(gates, checks)
        graph = project.graph()
        project.start(graph)
        run(graph, project, Command(resume=ATTEMPT))          # g1
        run(graph, project, Command(resume=ATTEMPT))          # g2
        self.assertEqual(project.values(graph)["effective"], ["g1", "g2"])
        graph.update_state({"configurable": {"thread_id": THREAD}}, {"target_gate": "g1"})
        run(graph, project, Command(resume=ATTEMPT))          # re-earn g1
        self.assertEqual(project.evidence_gates(graph), ["g1", "g2", "g1"])
        self.assertNotIn("g2", project.values(graph)["effective"])

    def test_escalate_is_terminal(self):
        gates = [GateSpec(id="g1", checks=("c1",), repair=RepairSpec(max_attempts=1)),
                 GateSpec(id="g2", needs=("g1",), checks=("c2",))]
        project = self.project(gates, [check("c1", "false"), check("c2", "true")])
        graph = project.graph()
        project.start(graph)
        answered = drive(graph, project, escalation="abandon")
        self.assertEqual([p["kind"] for p in answered], ["work", "escalation"])
        values = project.values(graph)
        self.assertEqual(values["status"], "abandoned")
        self.assertEqual([r["kind"] for r in values["evidence"]], ["escalation"])
        self.assertEqual(pending(graph), [])
        self.assertEqual(graph.get_state({"configurable": {"thread_id": THREAD}}).next, ())


if __name__ == "__main__":
    unittest.main()
