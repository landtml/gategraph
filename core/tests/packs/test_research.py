"""Tests for the research pack (brief D4; plan sections 1.7 and 1.13).

The gate walk uses the kernel the way an external actor would, in order:
config.load, packs.load_packs, checks.run_check, surface.hash_surface and
store_surface, evidence.make_record, then surface.effective_gates. The G7
unit graph does not exist yet, so the walk is written out here. No model is
used: the one human step is an approval record that the actor supplies.

The port of seed/checks/verify_claims.py is also run directly, so that every
way a claim can go missing is shown to fail rather than skip.

Runs with the system python3: standard library and PyYAML only.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import functools
import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

import fixtures_v2 as fx
from core.kernel import checks, config, evidence, packs, paths, surface
from core.kernel.globs import glob_match
from core.kernel.types import (Actor, ActorKind, CheckStatus, ProducerKind,
                               ProducerRecord, RecordKind, Severity, canonical_json)

PACK_DIR = pathlib.Path(__file__).resolve().parents[2] / "packs" / "research"
EXAMPLES = PACK_DIR / "examples"
GOOD = EXAMPLES / "good"
BROKEN = sorted(p for p in EXAMPLES.glob("broken-*") if p.is_dir())
VERIFY = PACK_DIR / "checks" / "verify_claims.py"

PRESET = ["scoped", "sourced", "composed", "verified"]
PACK_CHECKS = frozenset({"claims-have-sources", "claims-verified", "sources-resolve"})
MEMO_GLOBS = ("**/*.md",)
AT = "2026-11-13T09:00:00+00:00"
ACTOR_TOOL = Actor(kind=ActorKind.TOOL, id="external-actor", channel=None)
ACTOR_HUMAN = Actor(kind=ActorKind.HUMAN, id="test-reviewer", channel="tty")
PACK_IGNORE = shutil.ignore_patterns("examples", "__pycache__", "*.pyc")

# The adapter that names the research pack and walks its default preset.
ADAPTER = """\
schema: 2
project: research-pack-test
unit_noun: report
unit_dir: reports
packs: [research]
gates:
  - id: scoped
    approval: {required: true, channel: tty}
    surface: {include: ["scope.md"]}
  - id: sourced
    checks: [sources-resolve]
    surface: {include: ["sources/**"]}
  - id: composed
    checks: [claims-have-sources]
    surface: {include: ["report.md"]}
  - id: verified
    checks: [claims-verified]
    surface: {include: ["report.md", "sources/**"]}
"""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Project:
    """A throwaway project: the research pack installed, one unit from an example."""

    def __init__(self, base: Path, example):
        self.root = fx.make_project(base, "research")
        (self.root / "harness" / "adapter" / "adapter.yaml").write_text(ADAPTER, encoding="utf-8")
        shutil.rmtree(self.root / "packs")
        shutil.copytree(PACK_DIR, self.root / "packs" / "research", ignore=PACK_IGNORE)
        self.unit = fx.unit_path(self.root)
        if example is not None:
            shutil.copytree(example, self.unit, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("expect.yaml"))
        self.unit_rel = self.unit.relative_to(self.root).as_posix()


class Walk:
    """The outcome of one walk: gates recorded, the failing gate if any, the config."""

    def __init__(self, recorded, failure, cfg, state):
        self.recorded = recorded
        self.failure = failure      # None, or (gate id, [failing CheckResult])
        self.cfg = cfg
        self.state = state


def _findings_digest(findings):
    return None if not findings else _sha(canonical_json(findings))


def walk(proj: Project) -> Walk:
    """Walk the gates in order as an external actor. Stops at the first failing gate."""
    cfg = config.load(proj.root)
    packs.load_packs(cfg)
    state = paths.state_dir(proj.root)
    chain, recorded = [], []
    for gate in cfg.gates:
        surf = surface.hash_surface(proj.unit, gate.id, cfg)
        results = []
        for name in gate.checks:
            spec = cfg.check(name)
            globs = spec.on or MEMO_GLOBS
            memos = sorted(f"{proj.unit_rel}/{rel}" for rel in surf.files
                           if any(glob_match(g, rel) for g in globs))
            results.append(checks.run_check(spec, proj.root, files=memos, unit=proj.unit_rel))
        failing = [r for r in results if r.status is not CheckStatus.PASS]
        if failing:
            return Walk(recorded, (gate.id, failing), cfg, state)
        digest = surface.store_surface(state, surf)
        if gate.approval is not None:
            kind, actor = RecordKind.APPROVAL, ACTOR_HUMAN
            producers = [ProducerRecord(id=f"{gate.id}-approval", kind=ProducerKind.HUMAN,
                                        digest=_sha(f"approve {gate.id}"), exit=None,
                                        verdict=None, findings_digest=None)]
        else:
            kind, actor = RecordKind.GATE, ACTOR_TOOL
            producers = [ProducerRecord(id=r.id, kind=ProducerKind.CHECK, digest=r.digest,
                                        exit=r.exit, verdict=None,
                                        findings_digest=_findings_digest(r.findings))
                         for r in results]
        rec = evidence.make_record(chain, kind=kind, gate=gate.id, at=AT,
                                   surface_digest=digest, producers=producers,
                                   config_digest=config.config_digest(cfg), actor=actor)
        chain.append({**rec.to_dict(), evidence.SURFACE_KEY: surf.files})
        recorded.append(gate.id)
    lines = [json.dumps(line, sort_keys=True) for line in chain]
    (proj.unit / "evidence.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return Walk(recorded, None, cfg, state)


def effective(proj: Project, w: Walk):
    """(effective, stale) gate ids, read back from the evidence file on disk."""
    chain = fx.read_evidence(proj.unit)
    load = functools.partial(surface.load_surface, w.state)
    return (surface.effective_gates(chain, w.cfg, proj.unit, load),
            surface.stale_gates(chain, w.cfg, proj.unit, load))


def run_verify(cwd, *args):
    return subprocess.run([sys.executable, str(VERIFY), *args], cwd=cwd,
                          capture_output=True, text=True, timeout=60)


def rules_of(proc):
    """The rule of every JSON finding on stdout. A failing check must print only JSON."""
    return [json.loads(line)["rule"] for line in proc.stdout.splitlines() if line.strip()]


class _Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)

    def project(self, example):
        return Project(Path(tempfile.mkdtemp(dir=self.base)), example)


class PackDeclarationTest(_Base):
    def test_examples_are_present(self):
        self.assertTrue((GOOD / "report.md").is_file(), "examples/good/report.md is missing")
        self.assertGreaterEqual(len(BROKEN), 4, "the pack needs at least four broken examples")
        for example in BROKEN:
            self.assertTrue((example / "expect.yaml").is_file(), f"{example.name} has no expect.yaml")

    def test_pack_declares_the_default_preset_tools_and_checks(self):
        proj = self.project(GOOD)
        cfg = config.load(proj.root)
        readers = []
        loaded = {p.name: p for p in packs.load_packs(cfg, register_reader=readers.append)}
        pack = loaded["research"]
        self.assertEqual(pack.gate_presets["default"], tuple(PRESET))
        self.assertTrue({"fetch_url", "web_search", "read_file"} <= set(pack.tools))
        self.assertEqual({r.reader for r in readers}, {"pdf_text", "text"})
        self.assertEqual({c.id for c in pack.checks}, PACK_CHECKS)

    def test_adapter_gates_are_the_preset(self):
        cfg = config.load(self.project(GOOD).root)
        self.assertEqual([g.id for g in cfg.gates], PRESET)


class GateWalkTest(_Base):
    def test_good_example_passes_every_gate(self):
        proj = self.project(GOOD)
        w = walk(proj)
        self.assertIsNone(w.failure, f"the good example failed at {w.failure and w.failure[0]}")
        self.assertEqual(w.recorded, PRESET)
        eff, stale = effective(proj, w)
        self.assertEqual(eff, PRESET)
        self.assertEqual(stale, [])

    def test_editing_the_report_makes_its_gates_stale(self):
        proj = self.project(GOOD)
        w = walk(proj)
        report = proj.unit / "report.md"
        report.write_text(report.read_text(encoding="utf-8") + "\nAn added sentence.\n",
                          encoding="utf-8")
        eff, stale = effective(proj, w)
        self.assertEqual(eff, ["scoped", "sourced"])
        self.assertEqual(stale, ["composed", "verified"])

    def test_broken_examples_fail_at_the_named_gate_and_rule(self):
        for example in BROKEN:
            with self.subTest(example=example.name):
                expect = yaml.safe_load((example / "expect.yaml").read_text(encoding="utf-8"))
                w = walk(self.project(example))
                self.assertIsNotNone(w.failure, "a broken example must not complete every gate")
                gate_id, failing = w.failure
                self.assertEqual(gate_id, expect["fails_at"])
                self.assertEqual(w.recorded, PRESET[:PRESET.index(gate_id)])
                by_check = {r.id: r for r in failing}
                self.assertIn(expect["check"], by_check)
                found = [f for f in by_check[expect["check"]].findings if f.rule == expect["rule"]]
                self.assertTrue(found, f"no finding {expect['rule']!r} from {expect['check']!r}")
                self.assertTrue(all(f.severity is Severity.FATAL for f in found))

    def test_every_check_exits_on_contract_on_every_example(self):
        for example in [GOOD, *BROKEN]:
            proj = self.project(example)
            cfg = config.load(proj.root)
            packs.load_packs(cfg)
            names = [p.relative_to(proj.unit).as_posix() for p in proj.unit.rglob("*")
                     if p.is_file() and p.name not in ("evidence.jsonl", "manifest.yaml")]
            for spec in [c for c in cfg.checks if c.id in PACK_CHECKS]:
                with self.subTest(example=example.name, check=spec.id):
                    globs = spec.on or MEMO_GLOBS
                    memos = sorted(f"{proj.unit_rel}/{n}" for n in names
                                   if any(glob_match(g, n) for g in globs))
                    result = checks.run_check(spec, proj.root, files=memos, unit=proj.unit_rel)
                    self.assertNotIn(result.status, (CheckStatus.DID_NOT_RUN, CheckStatus.SKIPPED),
                                     result.findings)
                    self.assertIn(result.exit, (0, 1, 2))


class VerifyClaimsPortTest(_Base):
    """The ported script, run directly. Each case must fail, never skip."""

    SOURCE = "Net return 22.8 percent. Drawdown 9.4 percent.\n"
    BOTH = ("claim: net_return_pct = 22.8 | source: sources/fund.txt\n"
            "claim: max_drawdown_pct = 9.4 | source: sources/fund.txt\n")

    def make(self, memo_text, source_text=SOURCE):
        d = Path(tempfile.mkdtemp(dir=self.base))
        (d / "sources").mkdir()
        (d / "sources" / "fund.txt").write_text(source_text, encoding="utf-8")
        (d / "report.md").write_text(memo_text, encoding="utf-8")
        return d

    def test_complete_memo_passes(self):
        d = self.make("Prose.\n" + self.BOTH)
        proc = run_verify(d, "report.md", "--unit", ".", "--require", "net_return_pct",
                          "--require", "max_drawdown_pct")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_required_claim_missing_from_memo_fails(self):
        d = self.make("claim: net_return_pct = 22.8 | source: sources/fund.txt\n")
        proc = run_verify(d, "report.md", "--unit", ".", "--require", "net_return_pct",
                          "--require", "max_drawdown_pct")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("claims.missing_required", rules_of(proc))
        self.assertIn("max_drawdown_pct", proc.stdout)

    def test_actual_for_an_absent_claim_fails(self):
        # The seed passed here silently: the --actual was never compared.
        d = self.make("claim: net_return_pct = 22.8 | source: sources/fund.txt\n")
        proc = run_verify(d, "report.md", "--unit", ".", "--actual", "max_drawdown_pct=9.4")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("claims.missing_required", rules_of(proc))

    def test_refuted_actual_fails(self):
        d = self.make(self.BOTH)
        proc = run_verify(d, "report.md", "--unit", ".", "--actual", "net_return_pct=21.0")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("claims.refuted", rules_of(proc))

    def test_no_arguments_is_fatal_not_warn(self):
        # argparse exits 2 by default, and 2 is the warn code of the contract.
        proc = run_verify(self.base)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("claims.no_input", rules_of(proc))

    def test_memo_without_claims_fails(self):
        d = self.make("Prose only, every claim deleted.\n")
        proc = run_verify(d, "report.md", "--unit", ".", "--mode", "sources")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("claims.no_claims", rules_of(proc))

    def test_malformed_claim_line_fails(self):
        d = self.make("claim: net_return_pct = 22,8 | source: sources/fund.txt\n" +
                      self.BOTH.splitlines(True)[1])
        proc = run_verify(d, "report.md", "--unit", ".")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("claims.malformed", rules_of(proc))

    def test_bulleted_claim_line_is_not_skipped(self):
        d = self.make("- claim: net_return_pct = 22,8 | source: sources/fund.txt\n")
        proc = run_verify(d, "report.md", "--unit", ".", "--mode", "sources")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("claims.malformed", rules_of(proc))

    def test_claim_without_source_fails(self):
        d = self.make("claim: net_return_pct = 22.8\n")
        proc = run_verify(d, "report.md", "--unit", ".", "--mode", "sources")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("claims.no_source", rules_of(proc))

    def test_cited_source_that_is_absent_fails(self):
        d = self.make("claim: net_return_pct = 22.8 | source: sources/absent.txt\n")
        proc = run_verify(d, "report.md", "--unit", ".", "--mode", "sources")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("claims.source_missing", rules_of(proc))

    def test_value_not_in_cited_source_fails_in_values_mode(self):
        d = self.make("claim: net_return_pct = 21.0 | source: sources/fund.txt\n")
        proc = run_verify(d, "report.md", "--unit", ".", "--mode", "values")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("claims.not_in_source", rules_of(proc))

    def test_sources_mode_does_not_compare_values(self):
        d = self.make("claim: net_return_pct = 21.0 | source: sources/fund.txt\n")
        proc = run_verify(d, "report.md", "--unit", ".", "--mode", "sources")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_duplicate_claim_name_fails(self):
        d = self.make(self.BOTH + "claim: net_return_pct = 22.8 | source: sources/fund.txt\n")
        proc = run_verify(d, "report.md", "--unit", ".", "--mode", "sources")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("claims.duplicate", rules_of(proc))

    def test_unreadable_memo_fails(self):
        d = self.make(self.BOTH)
        proc = run_verify(d, "absent.md", "--unit", ".")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("claims.unreadable", rules_of(proc))


if __name__ == "__main__":
    unittest.main()
