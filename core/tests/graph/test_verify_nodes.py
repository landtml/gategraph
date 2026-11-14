"""G5: the blinded verifier (plan 1.5 run_verifier, judge_verdicts, verify_fanout; 1.11; bug B06).

Run standalone: ``.venv/bin/python core/tests/graph/test_verify_nodes.py``. Offline only:
every model is a FakeModel.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import hashlib
import typing
import unittest

from langgraph.graph import END, START, StateGraph

from core.agent.models import FakeModel, Structured
from core.graph.state import UnitState
from core.kernel.types import GateSpec, VerifierSpec
from core.graph.nodes.verify import (
    VerifierState,
    judge_verdicts,
    run_verifier,
    tally,
    verify_fanout,
)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


ARTIFACT = "ARTIFACT-BODY-ALPHA: the widget is 42 cm wide."
RUBRIC = "RUBRIC-CRITERIA-BETA: the width must be stated in metres."


def verdict_dict(verdict: str, findings=(), run: int = 1) -> dict:
    return {"id": "gate-x.v1", "run": run, "verdict": verdict,
            "findings": list(findings), "digest": "0" * 64,
            "artifact_sha256": sha(ARTIFACT)}


def fail_finding(rule: str, message: str = "line 1 says so") -> dict:
    return {"rule": rule, "severity": "fatal", "path": None, "line": None,
            "message": message, "remedy": None}


def make_cfg(model, *, n: int = 3, threshold: float = 0.5, artifact_sha: str | None = None):
    verifier = VerifierSpec(id="gate-kestrel.v1", rubric="rubric.md", n=n, model="m")
    gate = GateSpec(id="gate-kestrel", verifiers=(verifier,))
    return {
        "gates": [gate],
        "model_ports": {"m": model},
        "spread_threshold": threshold,
        "load_artifact": lambda gate_id: (ARTIFACT, artifact_sha or sha(ARTIFACT)),
        "load_rubric": lambda path: RUBRIC,
    }


class TestB06Blinding(unittest.TestCase):
    def test_B06_verifier_sees_only_artifact_and_rubric(self):
        model = FakeModel([Structured({"reasoning": "checked the width",
                                       "verdict": "pass", "findings": []})])
        cfg = make_cfg(model, n=1)
        # Everything the graph knows about the unit: identity, path, findings, history.
        state = {"target_gate": "gate-kestrel", "unit": "unit-kestrel",
                 "project": "proj-kestrel-9", "config_digest": "cfgdigest-kestrel",
                 "findings": [fail_finding("rule-kestrel", "PRIOR-FINDING-GAMMA")],
                 "history": ["HISTORY-DELTA earlier chat turn"],
                 "path": "/units/kestrel/sketch.md"}
        sends = verify_fanout(state, cfg)
        self.assertEqual(len(sends), 1)
        out = run_verifier(sends[0].arg, cfg)
        self.assertEqual(len(out["verdicts"]), 1)

        self.assertEqual(len(model.calls), 1)
        prompt = model.calls[0]
        self.assertIsInstance(prompt, str)
        self.assertIn(ARTIFACT, prompt)
        self.assertIn(RUBRIC, prompt)
        for leak in ("unit-kestrel", "proj-kestrel-9", "cfgdigest-kestrel", "gate-kestrel",
                     "gate-kestrel.v1", "/units/kestrel", "sketch.md", "PRIOR-FINDING-GAMMA",
                     "rule-kestrel", "HISTORY-DELTA"):
            self.assertNotIn(leak, prompt, f"prompt leaks {leak!r}")

    def test_subgraph_state_schema_is_artifact_rubric_verdict(self):
        keys = set(typing.get_type_hints(VerifierState))
        self.assertEqual(keys, {"artifact_text", "rubric_text", "verdict"})

    def test_verdict_with_mismatched_sha_is_discarded(self):
        model = FakeModel([Structured({"reasoning": "r", "verdict": "pass", "findings": []})])
        cfg = make_cfg(model, n=1, artifact_sha=sha("a different artifact"))
        sends = verify_fanout({"target_gate": "gate-kestrel"}, cfg)
        out = run_verifier(sends[0].arg, cfg)
        self.assertEqual(out, {"verdicts": []})
        self.assertEqual(model.calls, [], "a discarded verdict must not cost a model call")

    def test_missing_surface_sha_fails_closed(self):
        model = FakeModel([Structured({"reasoning": "r", "verdict": "pass", "findings": []})])
        cfg = make_cfg(model, n=1)
        payload = dict(verify_fanout({"target_gate": "gate-kestrel"}, cfg)[0].arg)
        del payload["artifact_sha256"]
        self.assertEqual(run_verifier(payload, cfg), {"verdicts": []})


class TestVerdictShape(unittest.TestCase):
    def test_uncited_finding_is_dropped(self):
        model = FakeModel([Structured({"reasoning": "r", "verdict": "fail", "findings": [
            {"criterion": "width-unit", "evidence": "line 1 states 42 cm",
             "severity": "fatal"},
            {"criterion": "no-evidence", "evidence": "  ", "severity": "fatal"},
            {"criterion": "missing-evidence-key", "severity": "fatal"},
        ]})])
        cfg = make_cfg(model, n=1)
        payload = verify_fanout({"target_gate": "gate-kestrel"}, cfg)[0].arg
        verdict = run_verifier(payload, cfg)["verdicts"][0]
        self.assertEqual(verdict["verdict"], "fail")
        self.assertEqual([f["rule"] for f in verdict["findings"]], ["width-unit"])
        self.assertEqual(verdict["findings"][0]["message"], "line 1 states 42 cm")

    def test_tie_is_a_did_not_run_verdict(self):
        model = FakeModel([Structured({"reasoning": "unsure", "verdict": "tie", "findings": []})])
        cfg = make_cfg(model, n=1)
        payload = verify_fanout({"target_gate": "gate-kestrel"}, cfg)[0].arg
        verdict = run_verifier(payload, cfg)["verdicts"][0]
        self.assertEqual(verdict["verdict"], "did_not_run")
        self.assertEqual(verdict["artifact_sha256"], sha(ARTIFACT))


class TestFanoutAndVote(unittest.TestCase):
    def _graph(self, cfg):
        graph = StateGraph(UnitState)
        graph.add_node("run_verifier", lambda payload: run_verifier(payload, cfg))
        graph.add_node("judge_verdicts", lambda state: judge_verdicts(state, cfg))
        graph.add_conditional_edges(START, lambda state: verify_fanout(state, cfg),
                                    ["run_verifier"])
        graph.add_edge("run_verifier", "judge_verdicts")
        graph.add_edge("judge_verdicts", END)
        return graph.compile()

    def test_three_verifiers_run_through_send_and_two_to_one_passes(self):
        model = FakeModel([
            Structured({"reasoning": "a", "verdict": "pass", "findings": []}),
            Structured({"reasoning": "b", "verdict": "pass", "findings": []}),
            Structured({"reasoning": "c", "verdict": "fail", "findings": [
                {"criterion": "width", "evidence": "line 1", "severity": "fatal"}]}),
        ])
        cfg = make_cfg(model, n=3, threshold=0.5)
        result = self._graph(cfg).invoke({"target_gate": "gate-kestrel", "verdicts": [],
                                          "findings": [], "status": "in_progress"})
        self.assertEqual(len(model.calls), 3)
        self.assertEqual(sorted(v["verdict"] for v in result["verdicts"]),
                         ["fail", "pass", "pass"])
        self.assertEqual(sorted(v["run"] for v in result["verdicts"]), [1, 2, 3])
        tallied = tally(result["verdicts"])
        self.assertEqual(tallied.majority, "pass")
        self.assertAlmostEqual(tallied.spread, 1 / 3)
        rules = [f["rule"] for f in result["findings"]]
        self.assertIn("verifier.spread", rules)
        self.assertNotIn("verifier.spread_escalate", rules)
        self.assertNotIn("width", rules, "a passing majority does not feed repair")

    def test_two_to_one_passes_and_records_the_spread(self):
        verdicts = [verdict_dict("pass"), verdict_dict("pass", run=2),
                    verdict_dict("fail", [fail_finding("width")], run=3)]
        cfg = {"spread_threshold": 0.5}
        out = judge_verdicts({"verdicts": verdicts, "findings": []}, cfg)
        spread = [f for f in out["findings"] if f["rule"] == "verifier.spread"]
        self.assertEqual(len(spread), 1)
        self.assertEqual(spread[0]["severity"], "warn")
        self.assertNotIn("verifier.spread_escalate",
                         [f["rule"] for f in out["findings"]])

    def test_spread_above_threshold_routes_to_escalate(self):
        verdicts = [verdict_dict("pass"), verdict_dict("fail", run=2),
                    verdict_dict("fail", run=3)]
        out = judge_verdicts({"verdicts": verdicts, "findings": []}, {"spread_threshold": 0.2})
        escalations = [f for f in out["findings"] if f["rule"] == "verifier.spread_escalate"]
        self.assertEqual(len(escalations), 1)
        self.assertEqual(escalations[0]["severity"], "fatal")

    def test_tie_and_no_votes_escalate_whatever_the_threshold(self):
        tie = [verdict_dict("pass"), verdict_dict("fail", run=2)]
        none = [verdict_dict("did_not_run"), verdict_dict("did_not_run", run=2)]
        for verdicts in (tie, none):
            out = judge_verdicts({"verdicts": verdicts, "findings": []}, {"spread_threshold": 1.0})
            self.assertIn("verifier.spread_escalate", [f["rule"] for f in out["findings"]])

    def test_failing_majority_feeds_fatal_findings_to_repair(self):
        verdicts = [verdict_dict("fail", [fail_finding("width")]),
                    verdict_dict("fail", [fail_finding("units")], run=2),
                    verdict_dict("pass", [fail_finding("ignored")], run=3)]
        out = judge_verdicts({"verdicts": verdicts, "findings": []}, {"spread_threshold": 0.5})
        rules = [f["rule"] for f in out["findings"]]
        self.assertIn("width", rules)
        self.assertIn("units", rules)
        self.assertNotIn("ignored", rules)


if __name__ == "__main__":
    unittest.main(verbosity=2)
