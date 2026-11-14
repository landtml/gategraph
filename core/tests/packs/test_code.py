"""Tests for the code pack (brief D2; plan section 1.13).

Runs with the system python3: the kernel is standard library and PyYAML only.
No graph and no model. The gates are walked the way an external actor would
walk them, with the kernel alone: config.load, packs.load_packs,
checks.run_check and select_checks, surface.hash_surface and store_surface,
evidence.make_record and verify_chain, and surface.effective_gates.
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

import fixtures_v2 as fx
from core.kernel import config
from core.kernel.checks import run_check, select_checks
from core.kernel.config import config_digest
from core.kernel.evidence import make_record, verify_chain
from core.kernel.packs import load_packs
from core.kernel.surface import effective_gates, hash_surface, load_surface, store_surface
from core.kernel.types import (Actor, ActorKind, CheckStatus, ProducerKind,
                               ProducerRecord, RecordKind)

PACK = fx.CORE / "packs" / "code"
EXAMPLES = PACK / "examples"
LEGACY_AGENTS = fx.CORE / "loop" / "agents"

# The default preset, in gate order. The adapter of every example declares
# exactly these gates, in this order.
PRESET = ["imported", "tested", "linted"]
BROKEN_EXAMPLES = {"broken-import", "broken-test", "broken-no-tests", "broken-lint"}

# sha256 of the legacy prompt files at the time they were moved (byte for byte).
PROMPT_DIGESTS = {
    "maker.md": "7f695a4e3fe88b32498c1e1541f4bd022a248d5779df45cf2e05a8d3ed9152f8",
    "checker.md": "ad5ff2c31cbcaddbabe246cde0592b281a98ebac2fc01892909862dce99e049d",
    "judge.md": "41ef33d8809217089b03098577ffb9104f4dd882a43aedc1befac2fdd671892f",
}

ACTOR = Actor(kind=ActorKind.TOOL, id="external-actor", channel=None)
_IGNORE = shutil.ignore_patterns("examples", "__pycache__", "*.pyc")


def _at(seq):
    return f"2026-11-13T00:00:{seq:02d}+00:00"


def build_project(example: Path, base: Path) -> Path:
    """A project root: the example's adapter, the code pack, and the example's unit."""
    root = fx.make_project(base, "minimal")
    shutil.copyfile(example / "adapter.yaml", root / "harness" / "adapter" / "adapter.yaml")
    shutil.copytree(PACK, root / "packs" / "code", ignore=_IGNORE)
    unit = fx.unit_path(root)
    unit.mkdir(parents=True, exist_ok=True)
    shutil.copytree(example / "unit", unit, dirs_exist_ok=True)
    return root


def unit_rel(cfg) -> str:
    return f"{cfg.unit_dir}/{fx.UNIT_SLUG}"


def walk(root: Path, state: Path):
    """Walk the gates in order as an external actor. Stop at the first gate whose checks do not all pass.

    Returns (cfg, chain, failed_gate, results). ``failed_gate`` is None when every
    gate passed; ``results`` are the CheckResults of the gate that failed.
    """
    cfg = config.load(root)
    unit = root / unit_rel(cfg)
    digest = config_digest(cfg)
    chain = []
    for gate in cfg.gates:
        surface_digest = store_surface(state, hash_surface(unit, gate.id, cfg.gates))
        specs = select_checks([cfg.check(name) for name in gate.checks])
        results = [run_check(spec, root, unit=unit_rel(cfg)) for spec in specs]
        if any(result.status != CheckStatus.PASS for result in results):
            return cfg, chain, gate.id, results
        producers = tuple(ProducerRecord(id=r.id, kind=ProducerKind.CHECK, digest=r.digest,
                                         exit=r.exit, verdict=None, findings_digest=None)
                          for r in results)
        chain.append(make_record(chain, kind=RecordKind.GATE, gate=gate.id, at=_at(len(chain)),
                                 surface_digest=surface_digest, producers=producers,
                                 config_digest=digest, actor=ACTOR))
    return cfg, chain, None, []


def unit_snapshot(unit: Path):
    """Every file under the unit with its bytes' sha256, plus any __pycache__ directory."""
    files = {}
    caches = []
    for path in sorted(unit.rglob("*")):
        if "__pycache__" in path.parts:
            caches.append(path.relative_to(unit).as_posix())
        elif path.is_file():
            files[path.relative_to(unit).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files, caches


class _ExampleTest(unittest.TestCase):
    def setUp(self):
        base = tempfile.TemporaryDirectory()
        self.addCleanup(base.cleanup)
        self.base = Path(base.name)
        self.count = 0

    def project(self, example: Path) -> Path:
        self.count += 1
        return build_project(example, self.base / f"p{self.count}")


class PackContract(_ExampleTest):
    def test_pack_loads_with_its_declared_contract(self):
        root = self.project(EXAMPLES / "good")
        cfg = config.load(root)
        registered = []
        packs = load_packs(cfg, register_reader=registered.append)
        self.assertEqual([p.name for p in packs], ["code"])
        pack = packs[0]
        self.assertEqual(pack.version, 1)
        self.assertEqual(pack.gate_presets, {"default": tuple(PRESET)})
        self.assertEqual([c.id for c in pack.checks], ["imports", "unit-tests", "lint"])
        self.assertEqual([r.reader for r in registered], ["text"])
        self.assertEqual(registered[0].match, ("**/*.py",))

    def test_adapter_declares_exactly_the_default_preset(self):
        for example in [EXAMPLES / "good", *sorted(EXAMPLES.glob("broken-*"))]:
            with self.subTest(example=example.name):
                cfg = config.load(self.project(example))
                self.assertEqual([gate.id for gate in cfg.gates], PRESET)
                self.assertEqual(cfg.gates[-1].seals_unit, True)

    def test_prompts_are_the_legacy_bytes(self):
        for name, digest in PROMPT_DIGESTS.items():
            with self.subTest(prompt=name):
                data = (PACK / "prompts" / name).read_bytes()
                self.assertEqual(hashlib.sha256(data).hexdigest(), digest)
                legacy = LEGACY_AGENTS / name
                if legacy.is_file():  # the legacy engine keeps its copy until cutover
                    self.assertEqual(data, legacy.read_bytes())


class GateWalk(_ExampleTest):
    def test_good_example_walks_every_gate_to_complete(self):
        root = self.project(EXAMPLES / "good")
        state = self.base / "state"
        before = unit_snapshot(root / unit_rel(config.load(root)))
        cfg, chain, failed, results = walk(root, state)
        self.assertIsNone(failed, [(r.id, r.status.value, [f.rule for f in r.findings])
                                   for r in results])
        self.assertEqual(len(chain), len(PRESET))
        verify_chain(chain)
        complete = effective_gates(chain, cfg, root / unit_rel(cfg),
                                   functools.partial(load_surface, state))
        self.assertEqual(complete, PRESET)
        # The checks wrote nothing into the unit: the surface the gates hashed is the surface now.
        self.assertEqual(unit_snapshot(root / unit_rel(cfg)), before)
        self.assertEqual(unit_snapshot(root / unit_rel(cfg))[1], [])

    def test_broken_examples_fail_at_the_named_gate_and_rule(self):
        self.assertEqual({p.name for p in EXAMPLES.glob("broken-*")}, BROKEN_EXAMPLES)
        for name in sorted(BROKEN_EXAMPLES):
            with self.subTest(example=name):
                example = EXAMPLES / name
                expect = yaml.safe_load((example / "expect.yaml").read_text(encoding="utf-8"))
                root = self.project(example)
                state = self.base / f"state-{name}"
                cfg, chain, failed, results = walk(root, state)
                self.assertEqual(failed, expect["gate"])
                passed = [gate.id for gate in cfg.gates[:PRESET.index(expect["gate"])]]
                self.assertEqual(effective_gates(chain, cfg, root / unit_rel(cfg),
                                                 functools.partial(load_surface, state)),
                                 passed)
                failing = [r for r in results if r.status != CheckStatus.PASS]
                self.assertEqual([r.id for r in failing], [expect["check"]])
                rules = [f.rule for r in failing for f in r.findings]
                self.assertIn(expect["rule"], rules)

    def test_every_check_exits_on_a_contract_code_for_every_example(self):
        for example in [EXAMPLES / "good", *sorted(EXAMPLES.glob("broken-*"))]:
            with self.subTest(example=example.name):
                root = self.project(example)
                cfg = config.load(root)
                load_packs(cfg)
                for spec in cfg.checks:
                    result = run_check(spec, root, unit=unit_rel(cfg))
                    self.assertIn(result.exit, (0, 1, 2), f"{spec.id}: exit {result.exit}")
                    self.assertNotEqual(result.status, CheckStatus.DID_NOT_RUN, spec.id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
