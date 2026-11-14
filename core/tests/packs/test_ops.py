"""Tests for the ops reference pack (brief D6; plan section 1.13).

Runs with the system python3: standard library and PyYAML only. The gates are
walked with the kernel the way an external actor would: config.load,
load_packs, checks.run_check, surface.hash_surface and store_surface,
evidence.make_record, surface.effective_gates. No model is used. Every project
is a temporary directory; the apply tool is run only with a temporary workspace.
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import functools
import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from core.kernel import checks, config, evidence, surface
from core.kernel.packs import BUILTIN_TOOLS, load_packs
from core.kernel.types import (
    Actor, ActorKind, CheckStatus, ProducerKind, ProducerRecord, RecordKind,
)
from core.packs.ops import planfile
from core.packs.ops import tool as ops_tool
from core.tools.registry import Toolbox, ToolRefused
from core.tools.spec import ToolContext

CORE = Path(__file__).resolve().parents[2]
PACK_DIR = CORE / "packs" / "ops"
EXAMPLES = PACK_DIR / "examples"
UNIT = "changes/unit-001"
FINAL_GATE = "approved"
AT = "2026-11-13T09:00:00+00:00"
KNOWN_TOOLS = BUILTIN_TOOLS | {ops_tool.NAME}
GOOD = "good"
BROKEN = sorted(p.name for p in EXAMPLES.iterdir() if p.name.startswith("broken-"))


def example_names():
    return [GOOD] + BROKEN


class _Base(unittest.TestCase):
    def setUp(self):
        base = tempfile.TemporaryDirectory()
        self.addCleanup(base.cleanup)
        self.base = Path(base.name)
        self.count = 0

    def project(self, example):
        """A new project root: the adapter, the ops pack, and one example unit."""
        self.count += 1
        root = self.base / f"project-{self.count}"
        (root / "harness" / "adapter").mkdir(parents=True)
        shutil.copyfile(EXAMPLES / "adapter.yaml", root / "harness" / "adapter" / "adapter.yaml")
        shutil.copytree(PACK_DIR, root / "packs" / "ops", ignore=shutil.ignore_patterns(
            "examples", "__pycache__", "*.pyc", "tests"))
        shutil.copytree(EXAMPLES / example / "changes", root / "changes")
        return root

    def state(self):
        """A new surface store and state directory, outside every project root."""
        self.state_count = getattr(self, "state_count", 0) + 1
        path = self.base / f"state-{self.state_count}"
        path.mkdir()
        return path


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Walk:
    """What an external actor saw: the chain, each gate's check results, and where it stopped."""

    def __init__(self, cfg, chain, results, stopped, effective):
        self.cfg, self.chain, self.results = cfg, chain, results
        self.stopped = stopped          # gate id that failed, or the approval it waits for
        self.effective = effective      # gate ids that are effective at the end


def walk(root, state_dir, *, approve=True):
    """Walk every gate of the project at ``root`` the way an external actor would."""
    cfg = config.load(root)
    load_packs(cfg, known_tools=KNOWN_TOOLS)
    unit_dir = root / UNIT
    config_digest = config.config_digest(cfg)
    chain, results, stopped = [], {}, None
    for gate in cfg.gates:
        specs = checks.select_checks([cfg.check(name) for name in gate.checks])
        outcome = tuple(checks.run_check(spec, root, unit=UNIT) for spec in specs)
        results[gate.id] = outcome
        surface_digest = surface.store_surface(
            state_dir, surface.hash_surface(unit_dir, gate.id, cfg.gates))
        if gate.approval is not None:
            if not approve:
                stopped = gate.id
                break
            chain.append(evidence.make_record(
                chain, kind=RecordKind.APPROVAL, gate=gate.id, at=AT,
                surface_digest=surface_digest,
                producers=(ProducerRecord(
                    id=f"{gate.id}.human", kind=ProducerKind.HUMAN,
                    digest=_sha(f"approve {gate.id}"), exit=None, verdict="approve",
                    findings_digest=None),),
                config_digest=config_digest,
                actor=Actor(kind=ActorKind.HUMAN, id="reviewer",
                            channel=gate.approval.channel)))
            continue
        if any(r.status in (CheckStatus.FATAL, CheckStatus.DID_NOT_RUN) for r in outcome):
            stopped = gate.id
            break
        chain.append(evidence.make_record(
            chain, kind=RecordKind.GATE, gate=gate.id, at=AT,
            surface_digest=surface_digest,
            producers=tuple(ProducerRecord(
                id=r.id, kind=ProducerKind.CHECK, digest=r.digest, exit=r.exit,
                verdict=None, findings_digest=None) for r in outcome),
            config_digest=config_digest,
            actor=Actor(kind=ActorKind.TOOL, id="external-actor", channel=None)))
    evidence.verify_chain(chain)
    effective = surface.effective_gates(
        chain, cfg, unit_dir, functools.partial(surface.load_surface, state_dir))
    return Walk(cfg, chain, results, stopped, effective)


class ExampleWalkTest(_Base):
    def test_good_example_walks_every_gate_to_complete(self):
        root = self.project(GOOD)
        state = self.state()
        result = walk(root, state)
        self.assertEqual(result.stopped, None)
        gate_ids = [gate.id for gate in result.cfg.gates]
        self.assertEqual(result.effective, gate_ids)
        for gate_id in gate_ids:
            for check in result.results[gate_id]:
                self.assertEqual(check.status, CheckStatus.PASS,
                                 f"{gate_id}/{check.id}: {check.findings}")
        self.assertEqual(len(result.chain), len(gate_ids))

    def test_good_adapter_names_the_default_preset(self):
        root = self.project(GOOD)
        cfg = config.load(root)
        pack = load_packs(cfg, known_tools=KNOWN_TOOLS)[0]
        self.assertEqual([gate.id for gate in cfg.gates], list(pack.gate_presets["default"]))
        final = cfg.gate(FINAL_GATE)
        self.assertIsNotNone(final.approval)
        self.assertTrue(final.approval.required)
        self.assertTrue(final.seals_unit)

    def test_final_gate_is_not_complete_without_its_approval(self):
        root = self.project(GOOD)
        result = walk(root, self.state(), approve=False)
        self.assertEqual(result.stopped, FINAL_GATE)
        self.assertNotIn(FINAL_GATE, result.effective)
        self.assertEqual(result.effective, ["planned", "dry_run"])

    def test_each_broken_example_fails_at_the_gate_and_rule_it_names(self):
        for name in BROKEN:
            with self.subTest(example=name):
                expect = yaml.safe_load((EXAMPLES / name / "expect.yaml").read_text(encoding="utf-8"))
                root = self.project(name)
                result = walk(root, self.state())
                self.assertEqual(result.stopped, expect["gate"])
                self.assertNotIn(expect["gate"], result.effective)
                named = [c for c in result.results[expect["gate"]] if c.id == expect["check"]]
                self.assertEqual(len(named), 1, f"check {expect['check']} did not run")
                self.assertEqual(named[0].status, CheckStatus.FATAL)
                rules = [finding.rule for finding in named[0].findings]
                self.assertIn(expect["rule"], rules)

    def test_every_check_exits_with_a_contract_code_on_every_example(self):
        for name in example_names():
            with self.subTest(example=name):
                root = self.project(name)
                cfg = config.load(root)
                for check in cfg.checks:
                    if not check.command.startswith("python3 packs/ops/"):
                        continue
                    result = checks.run_check(check, root, unit=UNIT)
                    self.assertIn(result.exit, (0, 1, 2), f"{check.id} exited {result.exit}")
                    self.assertNotEqual(result.status, CheckStatus.DID_NOT_RUN, check.id)
                    self.assertFalse([f for f in result.findings if f.rule == "check.output"],
                                     f"{check.id} wrote a traceback or stderr")


class PackDeclarationTest(_Base):
    def test_pack_declares_the_apply_tool_and_three_checks(self):
        doc = config.parse_document((PACK_DIR / "pack.yaml").read_text(encoding="utf-8"))
        self.assertIn(ops_tool.NAME, doc["tools"])
        self.assertEqual([c["id"] for c in doc["checks"]],
                         ["plan-parses", "plan-targets", "dry-run-clean"])
        self.assertEqual(doc["gate_presets"]["default"], ["planned", "dry_run", FINAL_GATE])

    def test_pack_loads_once_the_toolbox_names_the_apply_tool(self):
        cfg = config.load(self.project(GOOD))
        pack = load_packs(cfg, known_tools=KNOWN_TOOLS)[0]
        self.assertEqual(pack.name, "ops")
        self.assertEqual(pack.readers, ())
        self.assertEqual([c.id for c in pack.checks],
                         ["plan-parses", "plan-targets", "dry-run-clean"])


class ApplyToolTest(_Base):
    def setUp(self):
        super().setUp()
        self.root = self.project(GOOD)
        self.unit = self.root / UNIT
        self.snapshot_before = self._snapshot()

    def _snapshot(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file()}

    def _toolbox(self, journal=None):
        context = ToolContext(
            root=self.root, unit_dir=self.unit, scratch_dir=self.root / "scratch",
            writable=(UNIT,), blind=(), budget=_budget(),
            journal=journal or _Journal())
        return Toolbox([ops_tool.apply_spec()], context)

    def _changed(self):
        after = self._snapshot()
        return sorted(name for name in set(after) | set(self.snapshot_before)
                      if after.get(name) != self.snapshot_before.get(name))

    def test_apply_tool_is_needs_approval_and_not_idempotent(self):
        spec = ops_tool.apply_spec()
        self.assertEqual(spec.name, ops_tool.NAME)
        self.assertTrue(spec.needs_approval)
        self.assertFalse(spec.idempotent)
        self.assertEqual(spec.effects, frozenset({"write"}))

    def test_toolbox_refuses_apply_when_not_approved(self):
        toolbox = self._toolbox()
        with self.assertRaises(ToolRefused):
            toolbox.call(ops_tool.NAME, {}, thread_id="t1",
                         tool_call_id="c1", approved=False)
        self.assertFalse((self.unit / planfile.SANDBOX).exists())
        self.assertEqual(self._changed(), [])

    def test_approved_apply_writes_only_under_the_unit_sandbox(self):
        toolbox = self._toolbox()
        result = toolbox.call(ops_tool.NAME, {}, thread_id="t1",
                              tool_call_id="c1", approved=True)
        sandbox = self.unit / planfile.SANDBOX
        self.assertEqual((sandbox / "config" / "app.yaml").read_text(encoding="utf-8"),
                         "retries: 3\n")
        self.assertEqual((sandbox / "notes" / "status.txt").read_text(encoding="utf-8"), "ready\n")
        self.assertEqual(sorted(result.touched),
                         [f"{UNIT}/{planfile.SANDBOX}/config/app.yaml",
                          f"{UNIT}/{planfile.SANDBOX}/notes/status.txt"])
        changed = self._changed()
        self.assertTrue(changed)
        for name in changed:
            self.assertTrue(name.startswith(f"{UNIT}/{planfile.SANDBOX}/"), name)

    def test_apply_refuses_an_undeclared_target_even_when_approved(self):
        root = self.project("broken-undeclared-target")
        self.root, self.unit = root, root / UNIT
        self.snapshot_before = self._snapshot()
        with self.assertRaises(ToolRefused):
            self._toolbox().call(ops_tool.NAME, {}, thread_id="t1",
                                 tool_call_id="c1", approved=True)
        self.assertEqual(self._changed(), [])

    def test_apply_refuses_when_the_dry_run_log_has_an_error(self):
        root = self.project("broken-dry-run-error")
        self.root, self.unit = root, root / UNIT
        self.snapshot_before = self._snapshot()
        with self.assertRaises(ToolRefused):
            self._toolbox().call(ops_tool.NAME, {}, thread_id="t1",
                                 tool_call_id="c1", approved=True)
        self.assertEqual(self._changed(), [])

    def test_a_repeated_call_replays_the_recorded_result_and_writes_nothing(self):
        toolbox = self._toolbox()
        first = toolbox.call(ops_tool.NAME, {}, thread_id="t1",
                             tool_call_id="c1", approved=True)
        target = self.unit / planfile.SANDBOX / "notes" / "status.txt"
        target.write_text("edited after the first apply\n", encoding="utf-8")
        second = toolbox.call(ops_tool.NAME, {}, thread_id="t1",
                              tool_call_id="c1", approved=True)
        self.assertEqual(second, first)
        self.assertEqual(target.read_text(encoding="utf-8"), "edited after the first apply\n")

    def test_the_example_dry_run_log_is_what_the_plan_produces(self):
        plan = yaml.safe_load((EXAMPLES / GOOD / "changes" / "unit-001" / "plan.yaml")
                              .read_text(encoding="utf-8"))
        log = (EXAMPLES / GOOD / "changes" / "unit-001" / "dry_run.log").read_text(encoding="utf-8")
        self.assertEqual(planfile.dry_run_lines(plan), log.splitlines())


class _Journal:
    """An in-memory effects journal: the protocol of core.tools.spec.EffectsJournal."""

    def __init__(self):
        self._seen = {}

    def seen(self, key):
        return self._seen.get(key)

    def record(self, key, result):
        self._seen[key] = result


def _budget():
    from core.kernel.types import Budget
    return Budget(max_tool_calls=10)


if __name__ == "__main__":
    unittest.main()
