"""The data pack (brief D5): walked through the kernel the way an external actor would.

The actor loads the adapter (config.load), the pack (packs.load_packs), runs each
gate's checks (checks.run_check), and on a clean gate stores the surface and appends
one evidence record (surface.hash_surface, store_surface, evidence.make_record).
No model, no graph: the unit graph (G7) does not exist yet.

Expectations:
- examples/good passes every gate of the pack's default preset, and the chain
  leaves every gate effective;
- each examples/broken-* fails at the gate, check and finding rule named in its
  expect.yaml, and no gate after the failing one is sealed;
- every check exits 0, 1 or 2 on every example (the check exit-code contract).

Runs on the system python3: the kernel is standard library and PyYAML only.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import functools
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from core.kernel import config
from core.kernel.checks import run_check, select_checks
from core.kernel.evidence import SURFACE_KEY, make_record
from core.kernel.packs import load_packs
from core.kernel.surface import effective_gates, hash_surface, load_surface, store_surface
from core.kernel.types import CheckStatus, ProducerKind, ProducerRecord, RecordKind
from core.kernel.types import canonical_json

PACK_DIR = _bootstrap.REPO / "core" / "packs" / "data"
EXAMPLES = PACK_DIR / "examples"
UNIT = "datasets/unit-001"          # the adapter's unit_dir/unit slug, repo-relative
EXPECTED_PRESET = ("declared", "bounded", "reconciled")
EXPECTED_CHECKS = ("data-schema", "data-row-counts", "data-null-rates", "data-reconcile")
ACTOR = {"kind": "tool", "id": "external-actor", "channel": None}
CONTRACT_EXITS = (0, 1, 2)


def _at(seq):
    return f"2026-11-13T10:{seq:02d}:00+00:00"


def make_project(example, tmp):
    """A fresh project root: the example's adapter and unit, plus a copy of the pack.

    expect.yaml is the test's own file and is not part of the project.
    """
    root = Path(tmp) / example.name
    shutil.copytree(example, root, ignore=shutil.ignore_patterns("expect.yaml"))
    shutil.copytree(PACK_DIR, root / "packs" / "data",
                    ignore=shutil.ignore_patterns("examples", "__pycache__", "*.pyc"))
    return root


def read_chain(path):
    """The records of evidence.jsonl in file order, as the kernel reads them."""
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def walk_gates(root, cfg, preset):
    """The external actor: run each gate of the preset in order and seal it when clean.

    Returns ({gate id: [CheckResult]}, the first gate that did not pass or None).
    """
    state = root / "state"
    unit_dir = root / UNIT
    chain_path = unit_dir / "evidence.jsonl"
    config_digest = config.config_digest(cfg)
    outcomes = {}
    for gate_id in preset:
        gate = cfg.gate(gate_id)
        results = [run_check(cfg.check(name), root, unit=UNIT) for name in gate.checks]
        outcomes[gate_id] = results
        if any(result.status != CheckStatus.PASS for result in results):
            return outcomes, gate_id
        chain = read_chain(chain_path)
        surface = hash_surface(unit_dir, gate_id, cfg.gates)
        digest = store_surface(state, surface)
        producers = [ProducerRecord(id=r.id, kind=ProducerKind.CHECK, digest=r.digest,
                                    exit=r.exit, verdict=None, findings_digest=None)
                     for r in results]
        record = make_record(chain, kind=RecordKind.GATE, gate=gate_id, at=_at(len(chain)),
                             surface_digest=digest, producers=producers,
                             config_digest=config_digest, actor=ACTOR)
        line = record.to_dict()
        line[SURFACE_KEY] = dict(surface.files)
        with chain_path.open("a", encoding="utf-8") as fh:
            fh.write(canonical_json(line) + "\n")
    return outcomes, None


def effective(root, cfg):
    chain = read_chain(root / UNIT / "evidence.jsonl")
    surfaces = functools.partial(load_surface, root / "state")
    return effective_gates(chain, cfg, root / UNIT, surfaces)


class DataPackTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def example_dirs(self):
        return sorted(p for p in EXAMPLES.iterdir() if p.is_dir())

    def load(self, example_name):
        root = make_project(EXAMPLES / example_name, self.tmp)
        cfg = config.load(root)
        packs = load_packs(cfg)
        return root, cfg, packs

    def test_examples_exist(self):
        names = [p.name for p in self.example_dirs()]
        self.assertIn("good", names)
        self.assertTrue(any(n.startswith("broken-") for n in names), names)
        for name in names:
            if name.startswith("broken-"):
                self.assertTrue((EXAMPLES / name / "expect.yaml").is_file(), name)

    def test_pack_declares_its_checks_and_default_preset(self):
        _, cfg, packs = self.load("good")
        self.assertEqual(len(packs), 1)
        pack = packs[0]
        self.assertEqual(pack.name, "data")
        self.assertEqual(tuple(c.id for c in pack.checks), EXPECTED_CHECKS)
        self.assertEqual(pack.gate_presets["default"], EXPECTED_PRESET)
        self.assertEqual(tuple(g.id for g in cfg.gates), EXPECTED_PRESET)
        for check in pack.checks:
            script = check.command.split()[1]
            self.assertTrue((_bootstrap.REPO / "core" / script).is_file(), script)

    def test_checks_run_on_dataset_writes(self):
        _, cfg, packs = self.load("good")
        checks = packs[0].checks
        self.assertEqual(select_checks(checks, written=[f"{UNIT}/notes.md"]), ())
        triggered = select_checks(checks, written=[f"{UNIT}/rows.csv"])
        self.assertEqual(tuple(c.id for c in triggered), EXPECTED_CHECKS)

    def test_good_example_walks_every_gate(self):
        root, cfg, packs = self.load("good")
        outcomes, failed = walk_gates(root, cfg, packs[0].gate_presets["default"])
        self.assertIsNone(failed, failed)
        for gate_id in EXPECTED_PRESET:
            for result in outcomes[gate_id]:
                self.assertEqual(result.status, CheckStatus.PASS,
                                 (gate_id, result.id, result.findings))
                self.assertEqual(result.exit, 0)
        self.assertEqual(effective(root, cfg), list(EXPECTED_PRESET))

    def test_broken_examples_fail_at_named_gate_and_rule(self):
        broken = [p for p in self.example_dirs() if p.name.startswith("broken-")]
        self.assertTrue(broken)
        for example in broken:
            with self.subTest(example=example.name):
                expect = yaml.safe_load((example / "expect.yaml").read_text(encoding="utf-8"))
                root, cfg, packs = self.load(example.name)
                outcomes, failed = walk_gates(root, cfg, packs[0].gate_presets["default"])
                self.assertEqual(failed, expect["gate"])
                results = outcomes[failed]
                failing = [r for r in results if r.status != CheckStatus.PASS]
                self.assertEqual([r.id for r in failing], [expect["check"]],
                                 [(r.id, r.status) for r in results])
                self.assertEqual(failing[0].status, CheckStatus.FATAL)
                rules = [f.rule for f in failing[0].findings]
                self.assertIn(expect["rule"], rules)
                # Nothing after the failing gate is sealed, so it is not effective.
                gates = list(EXPECTED_PRESET)
                self.assertEqual(effective(root, cfg), gates[:gates.index(failed)])

    def test_every_check_exits_with_a_contract_code_on_every_example(self):
        for example in self.example_dirs():
            with self.subTest(example=example.name):
                root, cfg, packs = self.load(example.name)
                for pack in packs:
                    for check in pack.checks:
                        result = run_check(check, root, unit=UNIT)
                        self.assertIn(result.exit, CONTRACT_EXITS,
                                      (check.id, result.exit, result.findings))
                        self.assertNotEqual(result.status, CheckStatus.DID_NOT_RUN,
                                            (check.id, result.findings))
                        if example.name == "good":
                            self.assertEqual(result.exit, 0, (check.id, result.findings))


if __name__ == "__main__":
    unittest.main(verbosity=2)
