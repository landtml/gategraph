"""Tests for the document pack (brief D3; plan section 1.13).

The gates are walked here the way an external actor would, with the kernel and
no model: config.load reads the adapter, load_packs reads the pack, run_check
runs each gate's checks, hash_surface and store_surface take the unit's surface,
and make_record appends the gate record to the unit's evidence.jsonl.

Runs with the system python3: standard library and PyYAML only.
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import datetime
import functools
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

import fixtures_v2 as fx
from core.kernel import checks, config, evidence, surface
from core.kernel.packs import load_packs
from core.kernel.types import Actor, ActorKind, CheckStatus, ProducerKind, ProducerRecord, RecordKind

PACK_DIR = Path(_bootstrap.REPO) / "core" / "packs" / "document"
EXAMPLES = PACK_DIR / "examples"
PACK_CHECKS = {"required-sections", "no-placeholders", "page-count", "render"}
PRESET = ("scoped", "composed", "packaged")
ACTOR_ID = "external-actor"


def broken_examples():
    return sorted(p.name for p in EXAMPLES.glob("broken-*") if p.is_dir())


def make_project(base, example):
    """A project root: the shared adapter, this pack, and one example's unit."""
    root = Path(base)
    adapter = root / "harness" / "adapter"
    adapter.mkdir(parents=True)
    shutil.copyfile(EXAMPLES / "adapter.yaml", adapter / "adapter.yaml")
    shutil.copytree(PACK_DIR, root / "packs" / "document",
                    ignore=shutil.ignore_patterns("examples", "__pycache__"))
    shutil.copytree(EXAMPLES / example / "documents", root / "documents")
    return root


def walk(root):
    """Earn each gate in order, as an external actor.

    Returns (earned gate ids, failure). ``failure`` is None when every gate is
    earned, else (gate id, [CheckResult]) for the first gate whose checks did not
    all pass. Nothing is recorded for a failing gate.
    """
    cfg = config.load(root)
    unit = fx.unit_path(root)
    unit_rel = unit.relative_to(root).as_posix()
    state = root / ".state"
    chain = list(fx.read_evidence(unit))
    earned = []
    for gate in cfg.gates:
        results = [checks.run_check(cfg.check(cid), root, unit=unit_rel) for cid in gate.checks]
        if any(r.status is not CheckStatus.PASS for r in results):
            return earned, (gate.id, results)
        taken = surface.hash_surface(unit, gate.id, cfg.gates)
        digest = surface.store_surface(state, taken)
        producers = [ProducerRecord(id=r.id, kind=ProducerKind.CHECK, digest=r.digest,
                                    exit=r.exit, verdict=None, findings_digest=None)
                     for r in results]
        rec = evidence.make_record(
            chain, kind=RecordKind.GATE, gate=gate.id,
            at=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            surface_digest=digest, producers=producers,
            config_digest=config.config_digest(cfg),
            actor=Actor(kind=ActorKind.TOOL, id=ACTOR_ID, channel=None))
        line = rec.to_dict()
        line[evidence.SURFACE_KEY] = dict(taken.files)
        chain.append(line)
        with open(unit / "evidence.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, sort_keys=True) + "\n")
        earned.append(gate.id)
    return earned, None


class DocumentPackTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.count = 0

    def project(self, example):
        self.count += 1
        return make_project(self.base / f"p{self.count}", example)

    def test_pack_declares_its_checks_reader_and_default_preset(self):
        cfg = config.load(self.project("good"))
        registered = []
        packs = load_packs(cfg, register_reader=registered.append)
        self.assertEqual([p.name for p in packs], ["document"])
        pack = packs[0]
        self.assertEqual({c.id for c in pack.checks}, PACK_CHECKS)
        self.assertEqual(tuple(pack.gate_presets["default"]), PRESET)
        self.assertEqual(cfg.check("render").command, "python3 packs/document/checks/render.py {unit}")
        self.assertEqual([(r.reader, r.match) for r in registered], [("text", ("**/*.md",))])

    def test_good_example_walks_every_gate_of_the_default_preset(self):
        root = self.project("good")
        earned, failure = walk(root)
        self.assertIsNone(failure, failure and [(r.id, f.rule) for r in failure[1] for f in r.findings])
        self.assertEqual(tuple(earned), PRESET)
        cfg = config.load(root)
        unit = fx.unit_path(root)
        chain = fx.read_evidence(unit)
        state_of = functools.partial(surface.load_surface, root / ".state")
        self.assertEqual(tuple(surface.effective_gates(chain, cfg, unit, state_of)), PRESET)
        self.assertEqual(surface.stale_gates(chain, cfg, unit, state_of), [])
        self.assertTrue((unit / "build" / "document.html").is_file())

    def test_each_broken_example_fails_at_its_gate_with_the_named_rule(self):
        names = broken_examples()
        self.assertGreaterEqual(len(names), 4)
        for name in names:
            with self.subTest(example=name):
                expect = yaml.safe_load((EXAMPLES / name / "expect.yaml").read_text(encoding="utf-8"))
                earned, failure = walk(self.project(name))
                self.assertIsNotNone(failure, "the unit was accepted")
                gate, results = failure
                self.assertEqual(gate, expect["gate"])
                self.assertEqual(earned, list(PRESET[:PRESET.index(gate)]))
                failed = {(r.id, f.rule) for r in results if r.status is CheckStatus.FATAL
                          for f in r.findings}
                self.assertIn((expect["check"], expect["rule"]), failed)

    def test_every_check_exits_with_a_contract_code_on_every_example(self):
        for name in ["good"] + broken_examples():
            with self.subTest(example=name):
                root = self.project(name)
                cfg = config.load(root)
                unit_rel = fx.unit_path(root).relative_to(root).as_posix()
                for check_id in sorted(PACK_CHECKS):
                    result = checks.run_check(cfg.check(check_id), root, unit=unit_rel)
                    self.assertIn(result.exit, (0, 1, 2), f"{check_id} exit {result.exit}")
                    self.assertIn(result.status, (CheckStatus.PASS, CheckStatus.FATAL, CheckStatus.WARN))
                    if name == "good":
                        self.assertEqual(result.status, CheckStatus.PASS, check_id)


if __name__ == "__main__":
    unittest.main()
