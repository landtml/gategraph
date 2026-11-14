"""G7: the assembled unit graph (plan section 1.5; bugs B01, B03).

Run standalone: ``.venv/bin/python core/tests/graph/test_unit_graph.py``. Offline only: the
verifier model is a FakeModel and the work and approval answers come from the external actor
(this file), never a real model or a real client.

``Project`` and the drive helpers are shared with test_machinery.py.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from langgraph.types import Command, RetryPolicy

from core.agent.models import FakeModel, Structured
from core.graph import runner
from core.graph.nodes.approve import HumanAnswerRefused
from core.graph.nodes.work import register_work_actor
from core.graph.nodes.verify import sha256_text
from core.graph.state import NODE_NAMES
from core.graph.contracts import WorkResult
from core.graph.store import make_checkpointer
from core.graph.unit import UnknownUnit, build_unit_graph, initial_state
from core.kernel.config import Config
from core.kernel.faults import is_retryable
from core.kernel.types import (Approval, Budget, CheckSpec, GateSpec, RepairSpec,
                               Severity, Spend, VerifierSpec)

THREAD = "proj/u1"
ROOT = Path(__file__).resolve().parents[3]
DIGEST = hashlib.sha256(b"fixture configuration").hexdigest()
ATTEMPT = {"answer": "attempt", "by": "tester", "channel": "tty", "note": None}
APPROVE = {"answer": "approve", "by": "tester", "channel": "tty", "note": None}
ABANDON = {"answer": "abandon", "by": "tester", "channel": "tty", "note": "stop"}


def check(check_id, command):
    return CheckSpec(id=check_id, command=command, severity=Severity.FATAL, timeout_s=20.0)


def pass_verdict():
    return Structured({"reasoning": "the sketch meets the rubric", "verdict": "pass",
                       "findings": []})


class Project:
    """One unit in a temporary project, with a configuration and an in-memory checkpointer."""

    def __init__(self, root, gates, checks, *, verifier_votes=3, sketch=True):
        self.root = Path(root)
        self.unit_dir = self.root / "units" / "u1"
        self.state_dir = self.root / "state"
        self.lock_dir = self.root / "locks"
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        self.sketch = sketch
        self.artifact = self.unit_dir / "report.md"
        if sketch:
            self.artifact.write_text("sketch one\n", encoding="utf-8")
        self.config = Config(root=self.root, schema=2, project="proj", unit_noun="report",
                             unit_dir="units", packs=(), checks=tuple(checks),
                             gates=tuple(gates))
        self.model = FakeModel([pass_verdict() for _ in range(verifier_votes)])

    def cfg(self, **extra):
        text = self.artifact.read_text(encoding="utf-8") if self.sketch else ""
        cfg = {
            "config": self.config,
            "unit_dir": self.unit_dir,
            "state_dir": self.state_dir,
            "config_digest": DIGEST,
            "budget": Budget(max_iterations=200, max_model_calls=100),
            "load_artifact": lambda gate_id: (text, sha256_text(text)),
            "load_rubric": lambda path: "RUBRIC: the sketch must be clear.",
            "model_ports": {"reviewer": self.model},
            "spread_threshold": 0.0,
            "checkpointer": make_checkpointer({"in_memory": True}),
        }
        cfg.update(extra)
        return cfg

    def graph(self, **extra):
        return build_unit_graph(self.cfg(**extra))

    def start(self, graph, **state):
        base = initial_state(project="proj", unit="u1", config_digest=DIGEST)
        base.update(state)
        return run(graph, self, base)

    def values(self, graph):
        return graph.get_state({"configurable": {"thread_id": THREAD}}).values

    def evidence_gates(self, graph):
        return [rec["gate"] for rec in self.values(graph)["evidence"]]


def run(graph, project, value):
    return runner.run(graph, THREAD, value, lambda snapshot: None, lock_dir=project.lock_dir)


def pending(graph):
    return runner.pending(graph, THREAD)


def drive(graph, project, *, escalation="abandon", max_steps=40):
    """The external actor: answer each interrupt until the thread waits on nothing.

    Returns the payloads it answered, in order. ``escalation`` is the answer given to an
    escalation interrupt. Work is always answered with ``attempt``; approvals with ``approve``.
    """
    answered = []
    for _ in range(max_steps):
        items = pending(graph)
        if not items:
            return answered
        payload = items[0]
        answered.append(payload)
        kind = payload["kind"]
        if kind == "work":
            value = ATTEMPT
        elif kind == "approval":
            value = APPROVE
        elif kind == "escalation":
            value = {"answer": escalation, "by": "tester", "channel": "tty",
                     "note": "decided by the tester"}
        else:
            raise AssertionError(f"unexpected interrupt kind {kind!r}")
        run(graph, project, Command(resume=value))
    raise AssertionError("the external actor did not reach a resting point")


def three_gates():
    return ([GateSpec(id="g1", checks=("c1",), repair=RepairSpec(max_attempts=2)),
             GateSpec(id="g2", needs=("g1",), checks=("c2",),
                      approval=Approval(required=True, channel="tty")),
             GateSpec(id="g3", needs=("g2",), checks=("c3",),
                      verifiers=(VerifierSpec(id="g3.v1", rubric="rubric.md", n=3,
                                              model="reviewer"),))],
            [check("c1", "true"), check("c2", "true"), check("c3", "true")])


class UnitGraphTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.gates, self.checks = three_gates()
        self.project = Project(self._tmp.name, self.gates, self.checks)

    def test_compiled_graph_has_exactly_node_names(self):
        graph = self.project.graph()
        names = set(graph.get_graph().nodes) - {"__start__", "__end__"}
        self.assertEqual(names, set(NODE_NAMES))

    def test_retry_policy_on_run_verifier_and_work(self):
        graph = self.project.graph()
        for name in ("run_verifier", "work"):
            policies = graph.builder.nodes[name].retry_policy
            if isinstance(policies, RetryPolicy) or policies is None:
                policies = [policies]
            self.assertTrue(any(p is not None and p.retry_on is is_retryable for p in policies),
                            name)

    def test_B01_no_path_sets_a_gate_without_its_checks(self):
        # Structure: with judge_checks removed, seal is unreachable from start, so every
        # route to seal (direct, through approve, or through the verifiers) passes the checks.
        graph = self.project.graph()
        edges = [(e.source, e.target) for e in graph.get_graph().edges]
        seen, frontier = {"__start__"}, ["__start__"]
        while frontier:
            node = frontier.pop()
            for source, target in edges:
                if source == node and target != "judge_checks" and target not in seen:
                    seen.add(target)
                    frontier.append(target)
        self.assertNotIn("seal", seen)

        # Runtime: the walk seals every gate with the checks of that gate in the record.
        self.project.start(graph)
        drive(graph, self.project)
        records = self.project.values(graph)["evidence"]
        self.assertEqual([r["gate"] for r in records], ["g1", "g2", "g3"])
        for record, gate in zip(records, self.gates):
            producers = {p["id"] for p in record["producers"]}
            self.assertTrue(set(gate.checks) <= producers, (gate.id, producers))
            self.assertIn("policy-scan", producers)

    def test_three_gate_unit_walks_to_complete_with_external_actor(self):
        graph = self.project.graph()
        self.project.start(graph)
        answered = drive(graph, self.project)
        kinds = [p["kind"] for p in answered]
        self.assertEqual(kinds, ["work", "work", "approval", "work"])
        final = self.project.values(graph)
        self.assertEqual(final["status"], "complete")
        self.assertEqual(final["effective"], ["g1", "g2", "g3"])
        self.assertEqual(self.project.evidence_gates(graph), ["g1", "g2", "g3"])
        self.assertEqual(pending(graph), [])
        self.assertEqual(graph.get_state({"configurable": {"thread_id": THREAD}}).next, ())

    def test_failing_check_loops_to_work_then_escalates_after_max_attempts(self):
        gates = [GateSpec(id="g1", checks=("c1",), repair=RepairSpec(max_attempts=2))]
        project = Project(self._tmp.name + "/fail", gates, [check("c1", "false")])
        graph = project.graph()
        project.start(graph)
        answered = drive(graph, project, escalation="abandon")
        kinds = [p["kind"] for p in answered]
        self.assertEqual(kinds, ["work", "work", "escalation"])
        # The work request after the first failure carries the fatal finding as feedback.
        feedback = answered[1]["payload"]["findings"]
        self.assertTrue(any(f["severity"] == "fatal" for f in feedback), feedback)
        final = project.values(graph)
        self.assertEqual(final["status"], "abandoned")
        # The escalation decision is itself an evidence record (plan 1.12); no gate record.
        self.assertEqual([r["kind"] for r in final["evidence"]], ["escalation"])
        self.assertEqual(pending(graph), [])

    def test_B03_terminal_status_never_reaches_seal(self):
        graph = self.project.graph()
        self.project.start(graph, status="abandoned", target_gate="g1")
        self.assertEqual(pending(graph), [])
        self.assertEqual(self.project.values(graph)["evidence"], [])
        self.assertEqual(graph.get_state({"configurable": {"thread_id": THREAD}}).next, ())


class NeverFinishesActor:
    """A native work actor that never ends its step. It counts its calls."""

    def __init__(self, gate=None):
        self.calls = 0
        self.gate = gate

    def run(self, goal, findings, context):
        self.calls += 1
        return WorkResult(summary="still going", spend=Spend(), finished=self.gate is not None,
                          gate=self.gate)


class EscalationRoutingTest(unittest.TestCase):
    """Escalations with no target gate reach the escalate node (B02, B03 routing; plan 1.5)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_budget_exhausted_unit_pauses_at_escalation_then_abandon_records_it(self):
        gates, checks = three_gates()
        project = Project(self._tmp.name + "/budget", gates, checks)
        graph = project.graph(budget=Budget(max_iterations=1))
        project.start(graph)
        waiting = pending(graph)
        self.assertEqual([(w["kind"], w["gate"]) for w in waiting], [("escalation", None)])
        self.assertIn("backstop", waiting[0]["payload"]["reason"])
        self.assertEqual(project.values(graph)["evidence"], [])   # nothing recorded yet
        run(graph, project, Command(resume={"answer": "abandon", "by": "tester",
                                            "channel": "tty", "note": "stop"}))
        final = project.values(graph)
        self.assertEqual(final["status"], "abandoned")
        self.assertEqual([r["kind"] for r in final["evidence"]], ["escalation"])
        self.assertIsNone(final["evidence"][0]["gate"])
        self.assertEqual(pending(graph), [])

    def test_configuration_digest_change_escalates_before_any_gate(self):
        gates, checks = three_gates()
        project = Project(self._tmp.name + "/digest", gates, checks)
        other = hashlib.sha256(b"another configuration").hexdigest()
        graph = project.graph(config_digest=other)
        project.start(graph)
        waiting = pending(graph)
        self.assertEqual([(w["kind"], w["gate"]) for w in waiting], [("escalation", None)])
        self.assertIn("configuration digest changed", waiting[0]["payload"]["reason"])


class WorkFaultTest(unittest.TestCase):
    """A work step that returns a work.* fatal finding is a failed attempt (B01-style bound)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _run_actor(self, name, gate):
        actor = NeverFinishesActor(gate=gate)
        register_work_actor(name, actor)
        gates = [GateSpec(id="g1", checks=("c1",), repair=RepairSpec(max_attempts=2))]
        project = Project(f"{self._tmp.name}/{name}", gates, [check("c1", "true")])
        graph = project.graph(work_actor=name)
        project.start(graph, actor="native")
        return project, graph, actor

    def test_unfinished_native_actor_reaches_escalation_within_repair_bounds(self):
        project, graph, actor = self._run_actor("never-finishes-a", gate=None)
        waiting = pending(graph)
        self.assertEqual([(w["kind"], w["gate"]) for w in waiting], [("escalation", "g1")])
        # Two work calls = max_attempts, not the recursion limit or the budget. As on the failed
        # check path, attempts counts repairs: the attempt that escalates is not repaired.
        self.assertEqual(actor.calls, 2)
        values = project.values(graph)
        self.assertEqual(values["attempts"], {"g1": 1})
        self.assertEqual(values["fail_streak"], 1)
        self.assertIn("work.unfinished", [f["rule"] for f in values["findings"]])

    def test_native_actor_naming_unknown_gate_reaches_escalation_within_repair_bounds(self):
        project, graph, actor = self._run_actor("never-finishes-b", gate="no-such-gate")
        waiting = pending(graph)
        self.assertEqual([(w["kind"], w["gate"]) for w in waiting], [("escalation", "g1")])
        self.assertEqual(actor.calls, 2)
        self.assertIn("work.unknown_gate", [f["rule"] for f in project.values(graph)["findings"]])


# ---------------------------------------------------------------- golden comparisons
#
# tests/golden/scenarios.yaml records the LEGACY engine. The translator below applies each
# legacy step to the graph:
#
#   start              the first start of unit u1 starts the thread; later starts are the same thread
#   write/add/edit     file edits in the unit directory; a path outside the unit is refused
#   delete             file delete
#   approve G          the external actor answers the work interrupt for G (attempt), then the
#                      approval interrupt (approve). Only a gate with approval required can be approved.
#   tool_set G         the producing tool writes G's evidence file, then attempts G. Only the gate
#                      the graph is asking for can be named; any other G is refused (B01).
#   lint set_gate G    an attempt on G with no evidence file (the lint check decides).
#   lint (no set)      dropped: a check-only run has no graph action.
#   pause, proceed      dropped: the graph waits at its interrupt; nothing to do.
#   repair             dropped: manifest repair has no graph equivalent.
#   earn               expanded into approve, tool_set, lint, tool_set, tool_set (as make_golden does).
#   hash_extra         dropped: the surface of a gate is computed by the graph from the unit directory.
#   status             read only.
#
# Legacy ops with no graph action, asserted refused or absent in the tests below:
#   escalate, abandon (no human action at an arbitrary point; the escalation interrupt is the
#   graph's own), manifest_text, manifest_edit (the graph has no manifest), a second unit in the same
#   project (one thread per unit; the translator refuses it).
#
# Golden gates. Every gate records its surface (schema 2; B04). The legacy tool-set gates are
# composed, pdf_verified and packaged; each writes an evidence file (produces) that its own check
# requires. The lint check is lint_passed: at least one .tex file, and no tariff or PII marker.
#
# Not translated (asserted nowhere; reason per scenario in NOT_MODELLED): legacy rules whose
# expected stale or effective depends on an unhashed legacy gate (scope_confirmed and composed
# are unhashed in the legacy adapter, so an edit after them is ignored there; the graph hashes
# every gate). Also: human escalate, voluntary abandon, ACCT policy (no deny policy in the golden
# configuration), re-earning a gate that is already effective (needs an externally named target),
# multi-unit scheduling, and manifest-level operations.

GOLDEN_UNIT = "u1"
GOLDEN_CHAIN = ("scope_confirmed", "composed", "lint_passed", "pdf_verified", "packaged")
PRODUCERS = {"composed": "composed.ok", "pdf_verified": "pdf.ok", "packaged": "packaged.ok"}
_LINT_CODE = ("import sys; paths = sys.argv[1:]; "
              "texts = [open(p, encoding='utf-8', errors='replace').read() for p in paths]; "
              "ok = any(p.endswith('.tex') for p in paths) and "
              "not any('list price' in t or 'PII-MARKER' in t for t in texts); "
              "sys.exit(0 if ok else 1)")


def golden_gates(lint_repair=None):
    return [
        GateSpec(id="scope_confirmed", approval=Approval(required=True, channel="tty")),
        GateSpec(id="composed", needs=("scope_confirmed",), checks=("c_composed",),
                 produces=("composed.ok",)),
        GateSpec(id="lint_passed", needs=("composed",), checks=("c_lint",), repair=lint_repair),
        GateSpec(id="pdf_verified", needs=("lint_passed",), checks=("c_pdf",),
                 produces=("pdf.ok",)),
        GateSpec(id="packaged", needs=("pdf_verified",), checks=("c_pkg",),
                 produces=("packaged.ok",)),
    ]


def golden_checks():
    unit = f"units/{GOLDEN_UNIT}"
    lint = f'{sys.executable} -c "{_LINT_CODE}" {{files}}'
    return [check("c_composed", f"test -f {unit}/composed.ok"),
            check("c_lint", lint),
            check("c_pdf", f"test -f {unit}/pdf.ok"),
            check("c_pkg", f"test -f {unit}/packaged.ok")]


class LegacyTranslator:
    """Applies legacy steps (scenarios.yaml) to one unit graph. Records what it refused or dropped."""

    def __init__(self, project, graph):
        self.project = project
        self.graph = graph
        self.started = False
        self.refused: list[tuple] = []
        self.dropped: list[str] = []
        self.approvable = {g.id for g in project.config.gates
                           if g.approval is not None and g.approval.required}

    def apply(self, step):
        op = step["op"]
        unit = step.get("unit", GOLDEN_UNIT)
        if op == "start":
            if unit != GOLDEN_UNIT:
                self.refused.append((op, unit))              # one unit per project in this graph
            elif not self.started:
                self.project.start(self.graph)
                self.started = True
            else:
                self.dropped.append("start")                 # same thread: nothing to do
        elif op == "status":
            return
        elif op in ("pause", "proceed", "repair"):
            self.dropped.append(op)
        elif op == "lint" and not step.get("set_gate"):
            self.dropped.append(op)
        elif unit != GOLDEN_UNIT:
            self.refused.append((op, unit))
        elif op in ("write", "add", "edit", "delete"):
            self._file(step)
        elif op == "approve":
            self._gate(step["gate"], "approve")
        elif op == "tool_set":
            self._gate(step["gate"], "tool_set")
        elif op == "lint":
            self._gate(step["set_gate"], "lint")
        else:
            raise ValueError(f"no translation for legacy op {op!r}")

    def refresh(self):
        """Make the graph reconcile against the files now on disk.

        The graph reconciles at the start of each pass, so a file edit made between passes is not
        in ``effective`` or ``stale`` until one runs. A work interrupt answered with a non-attempt
        answer stays paused, and that pass is the reconcile. Legacy ``status`` read the disk."""
        if self.started and pending(self.graph) and pending(self.graph)[0]["kind"] == "work":
            run(self.graph, self.project, Command(resume={"answer": "wait"}))

    def _file(self, step):
        if not self.started:
            self.refused.append((step["op"], step["file"]))
            return
        unit_dir = self.project.unit_dir.resolve()
        path = (unit_dir / step["file"]).resolve()
        if unit_dir not in path.parents:
            self.refused.append((step["op"], step["file"]))  # a path outside the unit (B33)
            return
        if step["op"] == "delete":
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(step["content"], encoding="utf-8")

    def _gate(self, gate, mode):
        items = pending(self.graph) if self.started else []
        if mode == "approve" and gate not in self.approvable:
            self.refused.append((mode, gate))
            return
        if not items:
            self.refused.append((mode, gate))
            return
        first = items[0]
        if first["kind"] == "work" and first["gate"] == gate:
            if mode == "tool_set" and gate in PRODUCERS:
                self._write_evidence(PRODUCERS[gate])
            run(self.graph, self.project, Command(resume=ATTEMPT))
            if mode == "approve":
                after = pending(self.graph)
                if after and after[0]["kind"] == "approval" and after[0]["gate"] == gate:
                    run(self.graph, self.project, Command(resume=APPROVE))
                else:
                    self.refused.append(("approve-after-attempt", gate))
            return
        if mode == "approve" and first["kind"] == "approval" and first["gate"] == gate:
            run(self.graph, self.project, Command(resume=APPROVE))
            return
        self.refused.append((mode, gate))                    # the graph is not asking for gate

    def _write_evidence(self, name):
        path = self.project.unit_dir / name
        path.write_text("produced\n", encoding="utf-8")


def _expand_earn(steps):
    out = []
    for step in steps:
        if step["op"] != "earn":
            out.append(step)
            continue
        upto = GOLDEN_CHAIN.index(step["upto"])
        for gate in GOLDEN_CHAIN[:upto + 1]:
            if gate == "scope_confirmed":
                out.append({"op": "approve", "gate": gate})
            elif gate == "lint_passed":
                out.append({"op": "lint", "set_gate": gate})
            else:
                out.append({"op": "tool_set", "gate": gate})
    return out


def _golden_project(tmp, lint_repair=None):
    return Project(tmp, golden_gates(lint_repair), golden_checks(), sketch=False)


def translate(scenario_id, tmp):
    """Run one legacy scenario through the translator. Returns (project, graph, translator)."""
    scenario = SCENARIOS[scenario_id]
    project = _golden_project(tmp)
    graph = project.graph()
    translator = LegacyTranslator(project, graph)
    for step in _expand_earn(scenario["steps"]):
        translator.apply(step)
    translator.refresh()
    return project, graph, translator


def graph_triple(graph, started):
    """(status, effective, stale) of the unit graph; (None, None, None) if the unit does not exist."""
    if not started:
        return (None, None, None)
    values = graph.get_state({"configurable": {"thread_id": THREAD}}).values
    return (values.get("status"), values.get("effective"), values.get("stale"))


def legacy_triple(scenario_id):
    """(status, effective, stale) the snapshot records after the scenario's last step."""
    last = SNAPSHOTS[scenario_id]["steps"][-1]
    if "unit" in last:                                       # the last step names another unit
        return (None, None, None)
    return (last.get("status"), last.get("effective"), last.get("stale"))


def final_triple(sid, graph, translator):
    """The graph's (status, effective, stale) as the snapshot's last step describes them.

    A last step that names another unit describes a unit that does not exist here: (None, None,
    None) on both sides."""
    last_unit = SCENARIOS[sid]["steps"][-1].get("unit", GOLDEN_UNIT)
    if last_unit != GOLDEN_UNIT:
        return (None, None, None)
    return graph_triple(graph, translator.started)


def same_as_legacy(new, legacy):
    """Equal, with one documented mapping: all five gates effective and status in_progress is
    the legacy spelling of the new 'complete' status (the manifest never leaves in_progress)."""
    if new[0] == "complete" and legacy[0] == "in_progress" and legacy[1] == list(GOLDEN_CHAIN) \
            and not legacy[2]:
        return (new[1], new[2]) == (legacy[1], legacy[2])
    return tuple(new) == tuple(legacy)


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _scenarios():
    return {s["id"]: s for s in yaml.safe_load((GOLDEN / "scenarios.yaml").read_text(encoding="utf-8"))}


GOLDEN = ROOT / "tests" / "golden"
SCENARIOS = _scenarios()
SNAPSHOTS = {sid: _load(GOLDEN / "expected" / f"{sid}.json") for sid in SCENARIOS}

# Scenarios the translator covers and that must match the snapshot's final state.
MATCHING = (
    "start-fresh", "start-idempotent-preserves-evidence", "start-second-unit-blocked",
    "start-invalid-slug", "status-no-active", "pause-proceed", "repair-valid-manifest",
    "approve-unknown-unit", "approve-unknown-gate", "approve-tool-set-gate-refused",
    "complete-unit", "mach-out-of-order-refused", "stale-manifest-file-excluded",
    "lint-empty-unit-refused", "lint-fatal-tariffs-in-tex", "lint-warn-advisory-sets-gate",
    "lint-no-manifest", "lint-unknown-unit", "lint-disclosure-and-sections",
)

# Legacy-bug scenarios the translator covers; each must DIFFER from its snapshot.
BUG_TRANSLATED = (
    "bug-B01-lint-sets-any-gate", "bug-B01-lint-completes-chain",
    "bug-B04-hashed-not-toolset-approved-by-hand", "bug-B33-recorded-path-escapes-unit",
)

# Legacy-bug scenarios asserted by direct tests below (escalation, abandon, manifest).
BUG_DIRECT = (
    "bug-B02-pause-after-escalate-proceeds", "bug-B03-approve-on-escalated-unit",
    "bug-B03-lint-sets-gate-on-escalated-unit", "bug-B03-approve-on-abandoned-unit",
    "bug-B33-bare-string-record-passes", "bug-B33-bare-string-on-hashed-gate-crashes",
    "bug-B39-abandon-unknown-unit", "bug-B39-abandon-overwrites-escalated",
)

# Non-bug scenarios not translated, with the reason. Every scenario id is in exactly one list.
NOT_MODELLED = {
    "start-takes-over-paused": "multi-unit takeover",
    "proceed-blocked-by-other-active": "multi-unit scheduling",
    "proceed-abandoned-refused": "voluntary abandon (no graph action)",
    "start-abandoned-refused": "voluntary abandon (no graph action)",
    "escalate-is-terminal-for-start": "human escalate (no graph action)",
    "escalate-clears-active": "human escalate (no graph action)",
    "mach-escalate-terminal": "human escalate (no graph action)",
    "repair-corrupt-rebuilds": "manifest text (no manifest in the graph)",
    "corrupt-manifest-missing-keys": "manifest text (no manifest in the graph)",
    "mach-reset-invalidates-later": "re-earning an effective gate needs an externally named target",
    "mach-staleness-reopens": "edit after unhashed legacy gates scope_confirmed and composed",
    "stale-edit-reopens-downstream-chain": "edit after unhashed legacy gates",
    "stale-added-file-reopens-gate": "add after unhashed legacy gates",
    "stale-deleted-file-reopens-gate": "delete after unhashed legacy gates",
    "stale-relint-after-edit-recovers": "edit after unhashed legacy gates",
    "stale-nested-file-covered": "edit after unhashed legacy gates",
    "stale-unhashed-gate-ignores-edits": "legacy unhashed gate (B04 removes it)",
    "stale-declared-evidence-exempt-midflow": "declared-evidence exemption; add after unhashed gate",
    "stale-final-gate-not-exempt": "add after unhashed legacy gates",
    "lint-fatal-acct-blocks-gate": "ACCT policy not in the golden configuration; edit after unhashed gate",
    "lint-fail-streak-threshold": "ACCT policy; check-only lint",
}


class GoldenTranslationTest(unittest.TestCase):
    """Legacy scenarios through the translator, against tests/golden/expected/*.json.

    Coverage: MATCHING (19) and BUG_TRANSLATED (4) run through translate(); BUG_DIRECT (8) have
    their own tests in GoldenBugTest. The union with NOT_MODELLED is every scenario id.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_every_scenario_is_covered_or_listed_with_a_reason(self):
        covered = set(MATCHING) | set(BUG_TRANSLATED) | set(BUG_DIRECT)
        self.assertEqual(len(covered), len(MATCHING) + len(BUG_TRANSLATED) + len(BUG_DIRECT))
        self.assertFalse(covered & set(NOT_MODELLED))
        self.assertEqual(covered | set(NOT_MODELLED), set(SCENARIOS))
        self.assertGreaterEqual(len(MATCHING) + len(BUG_TRANSLATED) + len(BUG_DIRECT), 30)
        bugs = {sid for sid, s in SCENARIOS.items() if s.get("legacy_bug")}
        self.assertEqual(bugs, set(BUG_TRANSLATED) | set(BUG_DIRECT))

    def test_matching_scenarios_match_their_snapshots(self):
        for index, sid in enumerate(MATCHING):
            with self.subTest(scenario=sid):
                tmp = f"{self._tmp.name}/m{index}"
                _project, graph, translator = translate(sid, tmp)
                new = final_triple(sid, graph, translator)
                self.assertTrue(same_as_legacy(new, legacy_triple(sid)),
                                (new, legacy_triple(sid), translator.refused))

    def test_legacy_bug_scenarios_differ_as_documented(self):
        tmp = self._tmp.name
        # B01: lint sets any gate. The graph asks for composed; lint names no evidence file, so the
        # composed check fails and composed is not earned. Legacy earns it.
        _p, graph, t = translate("bug-B01-lint-sets-any-gate", f"{tmp}/b01a")
        status, effective, stale = graph_triple(graph, t.started)
        self.assertEqual(effective, ["scope_confirmed"])
        self.assertNotEqual(legacy_triple("bug-B01-lint-sets-any-gate")[1], effective)
        waiting = pending(graph)
        self.assertEqual([(w["kind"], w["gate"]) for w in waiting], [("work", "composed")])
        self.assertTrue(any(f["severity"] == "fatal" for f in waiting[0]["payload"]["findings"]))

        # B01: lint completes the chain. Each later gate is refused until the graph asks for it.
        _p, graph, t = translate("bug-B01-lint-completes-chain", f"{tmp}/b01b")
        self.assertEqual(graph_triple(graph, t.started)[1], ["scope_confirmed"])
        self.assertIn(("lint", "lint_passed"), t.refused)
        self.assertIn(("lint", "packaged"), t.refused)
        self.assertNotEqual(legacy_triple("bug-B01-lint-completes-chain")[1],
                            graph_triple(graph, t.started)[1])

        # B04: approval of a hashed gate by hand. The approval records scope_confirmed's surface, so
        # the later edit and add make it stale. Legacy keeps it effective.
        _p, graph, t = translate("bug-B04-hashed-not-toolset-approved-by-hand", f"{tmp}/b04")
        status, effective, stale = graph_triple(graph, t.started)
        self.assertEqual((effective, stale), ([], ["scope_confirmed"]))
        self.assertEqual(legacy_triple("bug-B04-hashed-not-toolset-approved-by-hand")[2], [])

        # B33: a recorded path outside the unit. The write outside the unit is refused, the
        # attempt on lint_passed seals the unit's own files only, and the later edit outside the
        # unit changes nothing. Legacy marks lint_passed stale.
        _p, graph, t = translate("bug-B33-recorded-path-escapes-unit", f"{tmp}/b33")
        status, effective, stale = graph_triple(graph, t.started)
        self.assertEqual(effective, ["scope_confirmed", "composed", "lint_passed"])
        self.assertEqual(stale, [])
        self.assertEqual(sum(1 for op, _ in t.refused if op in ("write", "edit")), 2)
        self.assertEqual(legacy_triple("bug-B33-recorded-path-escapes-unit")[2], ["lint_passed"])


class GoldenBugTest(unittest.TestCase):
    """Legacy-bug scenarios that need an escalation, an abandon, or a manifest, asserted directly.

    The escalating configuration has one gate whose check always fails, with one allowed repair.
    That is the graph's own route to the escalation interrupt. The legacy escalate op has no graph
    action (see the module notes), so each test reaches the escalation this way instead.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._n = 0

    def escalated(self):
        """A started unit paused at the escalation interrupt for scope_confirmed."""
        self._n += 1
        gates = [GateSpec(id="scope_confirmed", approval=Approval(required=True, channel="tty"),
                          checks=("c_fail",), repair=RepairSpec(max_attempts=1))]
        project = Project(f"{self._tmp.name}/e{self._n}", gates, [check("c_fail", "false")],
                          sketch=False)
        graph = project.graph()
        project.start(graph)
        run(graph, project, Command(resume=ATTEMPT))         # the one attempt; its check fails
        waiting = pending(graph)
        self.assertEqual([(w["kind"], w["gate"]) for w in waiting],
                         [("escalation", "scope_confirmed")])
        return project, graph

    def test_B02_pause_then_proceed_does_not_leave_escalation(self):
        # Legacy: escalate, pause, proceed puts the unit back in progress. New: nothing answers the
        # escalation but a recorded decision; a proceed with no answer waits at the same interrupt.
        project, graph = self.escalated()
        run(graph, project, None)
        self.assertEqual([(w["kind"], w["gate"]) for w in pending(graph)],
                         [("escalation", "scope_confirmed")])
        self.assertEqual(project.values(graph)["evidence"], [])

    def test_B03_approve_on_escalated_unit_is_refused(self):
        # Legacy: approve on the escalated unit set scope_confirmed. New: approve is not an answer
        # to an escalation, so it is refused and no gate record is written.
        project, graph = self.escalated()
        with self.assertRaises(HumanAnswerRefused):
            run(graph, project, Command(resume=APPROVE))
        self.assertEqual([(w["kind"], w["gate"]) for w in pending(graph)],
                         [("escalation", "scope_confirmed")])
        self.assertEqual(project.values(graph)["effective"], [])
        self.assertEqual(project.evidence_gates(graph), [])

    def test_B03_lint_on_escalated_unit_sets_no_gate(self):
        # Legacy: after escalate, lint set lint_passed. New: the escalation interrupt answers
        # only retry or abandon, so an attempt for lint_passed is refused.
        gates = golden_gates(lint_repair=RepairSpec(max_attempts=1))
        project = Project(f"{self._tmp.name}/lint", gates, golden_checks(), sketch=False)
        (project.unit_dir / "main.tex").parent.mkdir(parents=True, exist_ok=True)
        (project.unit_dir / "main.tex").write_text("list price 4.0\n", encoding="utf-8")
        graph = project.graph()
        project.start(graph)
        run(graph, project, Command(resume=ATTEMPT))         # scope_confirmed (approval asked next)
        run(graph, project, Command(resume=APPROVE))         # scope_confirmed earned
        waiting = pending(graph)
        self.assertEqual([(w["kind"], w["gate"]) for w in waiting], [("work", "composed")])
        (project.unit_dir / "composed.ok").write_text("produced\n", encoding="utf-8")
        run(graph, project, Command(resume=ATTEMPT))         # composed passes
        waiting = pending(graph)
        self.assertEqual([(w["kind"], w["gate"]) for w in waiting], [("work", "lint_passed")])
        run(graph, project, Command(resume=ATTEMPT))         # lint fails on GPA; no repair left
        self.assertEqual([(w["kind"], w["gate"]) for w in pending(graph)],
                         [("escalation", "lint_passed")])
        with self.assertRaises(HumanAnswerRefused):
            run(graph, project, Command(resume=ATTEMPT))
        values = project.values(graph)
        self.assertEqual(values["effective"], ["scope_confirmed", "composed"])
        self.assertNotIn("lint_passed", project.evidence_gates(graph))

    def test_B03_approve_on_abandoned_unit_has_nothing_to_answer(self):
        # Legacy: abandon, then approve set scope_confirmed on an abandoned unit. New: abandon is
        # an answer to the escalation; after it there is no approval interrupt and no gate.
        project, graph = self.escalated()
        run(graph, project, Command(resume=ABANDON))
        self.assertEqual(project.values(graph)["status"], "abandoned")
        self.assertEqual(pending(graph), [])
        self.assertEqual(project.values(graph)["effective"], [])

    def test_B33_bare_string_record_is_not_a_passed_gate(self):
        # Legacy: a bare-string gate record counted as passed. New: a record that is not a
        # digest-checked evidence record breaks the chain; the unit escalates with an integrity
        # reason and no gate is effective.
        gates = [GateSpec(id="scope_confirmed", approval=Approval(required=True, channel="tty"))]
        project = Project(f"{self._tmp.name}/b33a", gates, [], sketch=False)
        graph = project.graph()
        project.start(graph)
        graph.update_state({"configurable": {"thread_id": THREAD}}, {"evidence": ["yes"]})
        run(graph, project, Command(resume=ATTEMPT))
        waiting = pending(graph)
        self.assertEqual([w["kind"] for w in waiting], ["escalation"])
        self.assertIn("integrity", waiting[0]["payload"]["reason"])
        self.assertEqual(project.values(graph)["effective"], [])

    def test_B33_bare_string_on_hashed_gate_does_not_crash(self):
        # Legacy: AttributeError on a bare-string record of a hashed gate. New: the same integrity
        # escalation; the run returns normally.
        gates = [GateSpec(id="scope_confirmed", approval=Approval(required=True, channel="tty"))]
        project = Project(f"{self._tmp.name}/b33b", gates, [], sketch=False)
        graph = project.graph()
        project.start(graph)
        graph.update_state({"configurable": {"thread_id": THREAD}}, {"evidence": ["yes"]})
        run(graph, project, Command(resume=ATTEMPT))         # must not raise
        self.assertEqual([w["kind"] for w in pending(graph)], ["escalation"])

    def test_B39_abandon_of_unknown_unit_creates_nothing(self):
        # Legacy: abandon on a unit that does not exist printed "abandoned". New: there is no
        # thread, no interrupt to answer, and no state afterwards.
        gates = [GateSpec(id="scope_confirmed", approval=Approval(required=True, channel="tty"))]
        project = Project(f"{self._tmp.name}/b39a", gates, [], sketch=False)
        graph = project.graph()
        self.assertEqual(pending(graph), [])
        with self.assertRaises(UnknownUnit):
            run(graph, project, Command(resume=ABANDON))
        values = graph.get_state({"configurable": {"thread_id": THREAD}}).values
        self.assertIsNone(values.get("project"))             # no unit was created
        self.assertIsNone(values.get("status"))
        self.assertEqual(values.get("evidence", []), [])

    def test_B39_abandon_overwrites_escalated_only_through_a_recorded_answer(self):
        # Legacy: abandon silently overwrote escalated. New: the escalation is answered, the answer
        # is recorded as an escalation record and a decision, and the terminal status is set once.
        project, graph = self.escalated()
        run(graph, project, Command(resume=ABANDON))
        values = project.values(graph)
        self.assertEqual(values["status"], "abandoned")
        self.assertEqual([r["kind"] for r in values["evidence"]], ["escalation"])
        self.assertEqual(values["decisions"][-1]["answer"], "abandon")
        self.assertEqual(pending(graph), [])


CHILD_KILLED = 77


class KillAndProceedTest(unittest.TestCase):
    """The process that starts a unit is killed while the unit waits on work. A new process
    rebuilds the graph over the same SQLite file and finishes the unit (plan 1.15)."""

    def test_kill_while_waiting_on_work_then_proceed_in_a_new_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            gates, checks = three_gates()
            project = Project(tmp, gates, checks)
            child = subprocess.run([sys.executable, __file__, "--child-pause", str(project.root)],
                                   capture_output=True, text=True, timeout=300)
            self.assertEqual(child.returncode, CHILD_KILLED, child.stderr)
            # A new graph and a new checkpointer over the same file: nothing is shared with the child.
            graph = project.graph(checkpointer=make_checkpointer({"root": project.root}))
            waiting = pending(graph)
            self.assertEqual([(w["kind"], w["gate"]) for w in waiting], [("work", "g1")])
            answered = drive(graph, project)
            self.assertEqual([p["kind"] for p in answered], ["work", "work", "approval", "work"])
            final = project.values(graph)
            self.assertEqual(final["status"], "complete")
            self.assertEqual(project.evidence_gates(graph), ["g1", "g2", "g3"])


def _child_pause(root: str) -> None:
    """The child process: start the unit, reach the first work pause, then die without cleanup."""
    gates, checks = three_gates()
    project = Project(root, gates, checks)
    graph = project.graph(checkpointer=make_checkpointer({"root": root}))
    project.start(graph)
    if pending(graph)[0]["kind"] != "work":
        os._exit(1)
    os._exit(CHILD_KILLED)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--child-pause":
        _child_pause(sys.argv[2])
    unittest.main()
