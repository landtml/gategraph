"""Tests for core/graph/nodes/check.py: check_fanout, fan_out_checks, run_check, judge_checks (G3).

Builds a three-node graph (check_fanout, run_check, judge_checks) with the real
nodes and the kernel check runner, then checks the fan-out, the parallel run,
the fatal judgement, the warn-only pass, the clearing of a previous attempt and
the built-in policy scan.

Runs with .venv/bin/python (LangGraph 1.2.21).
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import json
import tempfile
import time
import unittest
from types import SimpleNamespace

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from core.graph.nodes.check import (
    POLICY_CHECK_ID,
    check_fanout,
    fan_out_checks,
    has_fatal_finding,
    judge_checks,
    run_check,
)
from core.graph.state import UnitState
from core.kernel import policy as kpolicy
from core.kernel.surface import hash_surface
from core.kernel.types import (
    CheckResult,
    CheckSpec,
    CheckStatus,
    GateSpec,
    Severity,
)


def build_graph():
    g = StateGraph(UnitState)
    g.add_node("check_fanout", check_fanout)
    g.add_node("run_check", run_check)
    g.add_node("judge_checks", judge_checks)
    g.add_edge(START, "check_fanout")
    g.add_conditional_edges("check_fanout", fan_out_checks, ["run_check"])
    g.add_edge("run_check", "judge_checks")
    g.add_edge("judge_checks", END)
    return g.compile()


class Env:
    """A temporary project: one unit with one gate, a set of checks, and a run config."""

    def __init__(self, root, checks, gate_checks, policies=()):
        self.root = pathlib.Path(root)
        self.unit_dir = self.root / "units" / "u1"
        self.state_dir = self.root / "state"
        self.unit_dir.mkdir(parents=True)
        (self.unit_dir / "spec.md").write_text("first line\nsecond line\n", encoding="utf-8")
        self.cfg = SimpleNamespace(
            root=self.root,
            checks=tuple(checks),
            gates=(GateSpec(id="g1", checks=tuple(gate_checks)),),
        )
        self.config = {"configurable": {
            "config": self.cfg,
            "unit_dir": self.unit_dir,
            "state_dir": self.state_dir,
            "policies": list(policies),
        }}

    def state(self, **extra):
        base = {"unit": "u1", "target_gate": "g1", "status": "in_progress"}
        base.update(extra)
        return base


def check(check_id, command, severity=Severity.FATAL):
    return CheckSpec(id=check_id, command=command, severity=severity, timeout_s=20.0)


class CheckNodesTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.app = build_graph()

    def env(self, checks, gate_checks=None, policies=()):
        ids = [c.id for c in checks] if gate_checks is None else gate_checks
        return Env(self._tmp.name, checks, ids, policies)

    def test_fanout_sends_one_per_check_plus_policy_and_stores_digest(self):
        env = self.env([check("c1", "true"), check("c2", "true"), check("c3", "true")])
        state = env.state()
        update = check_fanout(state, env.config)
        expected = hash_surface(env.unit_dir, "g1", env.cfg.gates).digest()
        self.assertEqual(update["surface_digest"], expected)
        self.assertTrue((env.state_dir / "surfaces" / f"{expected}.json").is_file())
        self.assertIsNone(update["check_results"])
        self.assertIsNone(update["verdicts"])

        sends = fan_out_checks({**state, **update}, env.config)
        self.assertEqual(len(sends), 4)
        self.assertTrue(all(isinstance(s, Send) and s.node == "run_check" for s in sends))
        self.assertEqual([s.arg["kind"] for s in sends], ["check", "check", "check", "policy"])
        self.assertEqual([s.arg["check_id"] for s in sends[:3]], ["c1", "c2", "c3"])
        self.assertTrue(all(s.arg["surface_digest"] == expected for s in sends))

        out = self.app.invoke(state, env.config)
        self.assertEqual(out["surface_digest"], expected)
        ids = sorted(item["id"] for item in out["check_results"])
        self.assertEqual(ids, sorted(["c1", "c2", "c3", POLICY_CHECK_ID]))

    def test_five_checks_run_in_parallel_and_all_results_arrive(self):
        checks_5 = [check(f"p{i}", "sleep 1.0") for i in range(5)]
        env = self.env(checks_5)
        started = time.monotonic()
        out = self.app.invoke(env.state(), env.config)
        elapsed = time.monotonic() - started
        ids = sorted(item["id"] for item in out["check_results"])
        self.assertEqual(ids, sorted([f"p{i}" for i in range(5)] + [POLICY_CHECK_ID]))
        self.assertTrue(all(item["status"] == "pass" for item in out["check_results"]))
        # Sequential runs would take at least 5 s; parallel runs take about 1 s.
        self.assertLess(elapsed, 4.0, f"checks ran one after another ({elapsed:.2f} s)")

    def test_did_not_run_result_makes_judge_report_fatal(self):
        env = self.env([check("good", "true"), check("broken", "exit 3")])
        out = self.app.invoke(env.state(), env.config)
        by_id = {item["id"]: item for item in out["check_results"]}
        self.assertEqual(by_id["broken"]["status"], "did_not_run")
        self.assertEqual(by_id["good"]["status"], "pass")
        self.assertTrue(has_fatal_finding(out["findings"]))
        rules = [f["rule"] for f in out["findings"] if f["severity"] == "fatal"]
        self.assertIn("check.did_not_run", rules)

    def test_fatal_exit_without_output_still_reports_fatal(self):
        env = self.env([check("silent", "exit 1")])
        out = self.app.invoke(env.state(), env.config)
        self.assertEqual({i["id"]: i["status"] for i in out["check_results"]}["silent"], "fatal")
        self.assertTrue(has_fatal_finding(out["findings"]))

    def test_warn_only_attempt_passes_with_findings_kept(self):
        payload = json.dumps({"rule": "style.long-line", "severity": "warn",
                              "path": "spec.md", "line": 1, "message": "line is long"})
        env = self.env([check("style", f"printf '%s\\n' '{payload}'; exit 2")])
        out = self.app.invoke(env.state(), env.config)
        by_id = {item["id"]: item for item in out["check_results"]}
        self.assertEqual(by_id["style"]["status"], "warn")
        self.assertEqual(by_id[POLICY_CHECK_ID]["status"], "pass")
        self.assertFalse(has_fatal_finding(out["findings"]))
        messages = [f["message"] for f in out["findings"]]
        self.assertEqual(messages, ["line is long"])

    def test_previous_attempt_results_are_cleared(self):
        env = self.env([check("c1", "true")])
        stale = CheckResult(id="stale-old", exit=0, status=CheckStatus.PASS, findings=(),
                            duration_s=0.0, digest="0" * 64).to_dict()
        out = self.app.invoke(env.state(check_results=[stale], verdicts=[{"id": "old"}]),
                              env.config)
        ids = sorted(item["id"] for item in out["check_results"])
        self.assertEqual(ids, sorted(["c1", POLICY_CHECK_ID]))
        self.assertNotIn("stale-old", ids)
        self.assertEqual(out.get("verdicts"), [])

    def test_builtin_policy_scan_reads_the_surface_and_fails_the_gate(self):
        policies = kpolicy.compile_policies(SimpleNamespace(deny=[{
            "id": "marker.leak", "regex": "ZX-MARK-7", "severity": "fatal",
            "scope": ["any"], "summary": "marker leaked",
        }]))
        env = self.env([], gate_checks=[], policies=policies)
        (env.unit_dir / "spec.md").write_text("ok\nZX-MARK-7 here\n", encoding="utf-8")
        out = self.app.invoke(env.state(), env.config)
        self.assertEqual([i["id"] for i in out["check_results"]], [POLICY_CHECK_ID])
        self.assertEqual(out["check_results"][0]["status"], "fatal")
        self.assertTrue(has_fatal_finding(out["findings"]))
        found = [f for f in out["findings"] if f["rule"] == "marker.leak"]
        self.assertEqual(found[0]["line"], 2)
        self.assertEqual(found[0]["path"], "spec.md")

    def test_B11_unscanned_pdf_is_not_silent(self):
        # Point the PDF extractor at a command that does not exist, so the
        # policy scan must skip the PDF with a skipped_pdf note.
        policies = kpolicy.compile_policies(SimpleNamespace(deny=[{
            "id": "marker.leak", "regex": "ZX-MARK-7", "severity": "fatal",
            "scope": ["any"], "summary": "marker leaked",
        }]))
        env = self.env([], gate_checks=[], policies=policies)
        env.config["configurable"]["pdf_command"] = ["no-such-pdf-extractor-b11", "{path}", "-"]
        (env.unit_dir / "doc.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
        out = self.app.invoke(env.state(), env.config)
        policy = {i["id"]: i for i in out["check_results"]}[POLICY_CHECK_ID]
        self.assertNotEqual(policy["status"], "pass", "an unscanned PDF passed silently")
        self.assertEqual(policy["status"], "fatal")
        found = [f for f in out["findings"] if f["rule"] == "policy.unscanned_artifact"]
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["severity"], "fatal")
        self.assertEqual(found[0]["path"], "doc.pdf")
        self.assertIn("doc.pdf", found[0]["message"])
        self.assertIn("pdftotext", found[0]["remedy"])
        self.assertIn("surface", found[0]["remedy"])
        self.assertTrue(has_fatal_finding(out["findings"]))

    def test_B11_unknown_binary_is_hashed_not_a_failure(self):
        # Section 1.9: binaries are hashed but not scanned, so a skipped_binary
        # note is not a policy failure.
        policies = kpolicy.compile_policies(SimpleNamespace(deny=[{
            "id": "marker.leak", "regex": "ZX-MARK-7", "severity": "fatal",
            "scope": ["any"], "summary": "marker leaked",
        }]))
        env = self.env([], gate_checks=[], policies=policies)
        (env.unit_dir / "blob.bin").write_bytes(b"\x00\x01ZX-MARK-7\x02")
        out = self.app.invoke(env.state(), env.config)
        policy = {i["id"]: i for i in out["check_results"]}[POLICY_CHECK_ID]
        self.assertEqual(policy["status"], "pass")
        self.assertFalse([f for f in out["findings"] if f["rule"] == "policy.unscanned_artifact"])
        self.assertFalse(has_fatal_finding(out["findings"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
