"""Offline verification of a unit in five steps (plan 1.8, bug B05 detection half).

Runs on the system python3 (kernel rules): standard library and PyYAML only.
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

from core.kernel import paths
from core.kernel.config import Config
from core.kernel.evidence import make_record
from core.kernel.projection import write_projections
from core.kernel.surface import hash_surface, load_surface, store_surface
from core.kernel.types import GateSpec, SurfaceSpec
from core.kernel.verify import VerifyReport, verify_unit

CONFIG = "c" * 64
ACTOR = {"kind": "tool", "id": "test-tool", "channel": None}
MAC_KEY = "k" * 32
GATES = (
    GateSpec(id="composed", surface=SurfaceSpec(include=("sketch.md",))),
    GateSpec(id="lint", needs=("composed",), surface=SurfaceSpec(include=("**",))),
    GateSpec(id="packaged", needs=("lint",), seals_unit=True),
)
NOT_CHECKED_DB = "not checked (no database)"


def _at(seq):
    return f"2026-01-01T00:00:{seq:02d}+00:00"


class Unit:
    """A sealed unit: files, state directory, chain and projections, as the runner leaves them."""

    def __init__(self, root):
        self.root = Path(root)
        self.unit = self.root / "units" / "u1"
        self.unit.mkdir(parents=True)
        self.state = paths.state_dir(self.root)
        self.cfg = Config(root=self.root, schema=2, project="demo", unit_noun="unit",
                          unit_dir="units", packs=(), checks=(), gates=GATES)
        self.chain = []
        self.surfaces = functools.partial(load_surface, self.state)

    def write(self, name, text):
        path = self.unit / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def seal(self, gate, mac_key=None):
        digest = store_surface(self.state, hash_surface(self.unit, gate, GATES))
        self.chain.append(make_record(
            self.chain, kind="gate", gate=gate, at=_at(len(self.chain)),
            surface_digest=digest, producers=(), config_digest=CONFIG, actor=ACTOR,
            mac_key=mac_key))

    def project(self, status="in_progress", checkpoint_id="ckpt-1"):
        write_projections(self.unit, self.cfg, self.chain, thread_id="demo/u1",
                          status=status, surfaces=self.surfaces, config_digest=CONFIG,
                          checkpoint_id=checkpoint_id)

    def verify(self, **kwargs):
        return verify_unit(self.unit, self.cfg, **kwargs)

    def evidence_lines(self):
        return (self.unit / "evidence.jsonl").read_text(encoding="utf-8").splitlines()

    def rewrite_evidence(self, mutate):
        lines = [json.loads(line) for line in self.evidence_lines()]
        mutate(lines)
        (self.unit / "evidence.jsonl").write_text(
            "".join(json.dumps(obj) + "\n" for obj in lines), encoding="utf-8")

    def manifest(self):
        return yaml.safe_load((self.unit / "manifest.yaml").read_text(encoding="utf-8"))

    def write_manifest(self, data):
        (self.unit / "manifest.yaml").write_text(
            yaml.safe_dump(data, sort_keys=True), encoding="utf-8")

    def store_file(self, gate_index):
        digest = self.chain[gate_index].surface_digest
        return self.state / "surfaces" / f"{digest}.json"


class _Check(unittest.TestCase):

    def assertFailsAt(self, report, step):
        self.assertFalse(report.ok, report)
        self.assertEqual(report.failed_step, step, report)
        self.assertEqual(report.steps[step - 1].status, "fail")


class _Base(_Check):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.u = Unit(self._tmp.name)
        self.u.write("sketch.md", "first sketch\n")
        self.u.seal("composed")
        self.u.write("lint.txt", "clean\n")
        self.u.seal("lint")
        self.u.seal("packaged")
        self.u.project()


class TestUntouchedUnit(_Base):

    def test_untouched_unit_passes(self):
        report = self.u.verify()
        self.assertIsInstance(report, VerifyReport)
        self.assertTrue(report.ok, report)
        self.assertIsNone(report.failed_step)
        self.assertEqual([s.status for s in report.steps[:2]], ["pass", "pass"])
        self.assertEqual(report.steps[3].status, "pass")

    def test_step3_is_not_checked_without_a_reader(self):
        report = self.u.verify()
        self.assertEqual(report.steps[2].status, NOT_CHECKED_DB)

    def test_step5_is_not_checked_without_a_key(self):
        report = self.u.verify()
        self.assertTrue(report.steps[4].status.startswith("not checked"))


class TestStep1(_Base):

    def test_edited_evidence_fails_step_1(self):
        def edit(lines):
            lines[1]["gate"] = "packaged"
        self.u.rewrite_evidence(edit)
        self.assertFailsAt(self.u.verify(), 1)

    def test_missing_evidence_with_a_manifest_head_fails_step_2(self):
        (self.u.unit / "evidence.jsonl").unlink()
        self.assertFailsAt(self.u.verify(), 2)


class TestStep2(_Base):

    def test_B05_edited_manifest_detected(self):
        data = self.u.manifest()
        data["head"]["digest"] = "e" * 64
        self.u.write_manifest(data)
        report = self.u.verify()
        self.assertFailsAt(report, 2)
        self.assertIn("head", report.failed_detail)

    def test_edited_gate_table_fails_step_2(self):
        data = self.u.manifest()
        data["gates"]["lint"]["seq"] = 0
        self.u.write_manifest(data)
        self.assertFailsAt(self.u.verify(), 2)


class TestStep3(_Base):

    def reader_for(self, evidence, status="in_progress"):
        calls = []

        def reader(thread_id, checkpoint_id):
            calls.append((thread_id, checkpoint_id))
            return {"evidence": evidence, "status": status}
        reader.calls = calls
        return reader

    def test_matching_checkpoint_passes(self):
        reader = self.reader_for(list(self.u.chain))
        report = self.u.verify(checkpoint_reader=reader)
        self.assertTrue(report.ok, report)
        self.assertEqual(report.steps[2].status, "pass")
        self.assertEqual(reader.calls, [("demo/u1", "ckpt-1")])

    def test_checkpoint_with_a_different_chain_fails_step_3(self):
        report = self.u.verify(checkpoint_reader=self.reader_for(list(self.u.chain[:2])))
        self.assertFailsAt(report, 3)

    def test_missing_checkpoint_fails_step_3(self):
        report = self.u.verify(checkpoint_reader=lambda thread_id, cid: None)
        self.assertFailsAt(report, 3)

    def test_edited_manifest_status_fails_step_3(self):
        data = self.u.manifest()
        data["status"] = "complete"
        self.u.write_manifest(data)
        report = self.u.verify(checkpoint_reader=self.reader_for(list(self.u.chain)))
        self.assertFailsAt(report, 3)


class TestStep4(_Base):

    def test_edited_artifact_fails_step_4(self):
        self.u.write("sketch.md", "a different sketch\n")
        report = self.u.verify()
        self.assertFailsAt(report, 4)
        self.assertIn("composed", report.failed_detail)
        self.assertNotIn("surface", report.failed_detail)

    def test_deleted_store_file_without_evidence_copy_is_surface_missing(self):
        self.u.store_file(0).unlink()
        def strip(lines):
            for obj in lines:
                obj.pop("surface", None)
        self.u.rewrite_evidence(strip)
        report = self.u.verify()
        self.assertFailsAt(report, 4)
        self.assertIn("surface missing", report.failed_detail)
        self.assertNotIn("stale", report.failed_detail)

    def test_edited_store_file_is_surface_corrupt(self):
        self.u.store_file(0).write_text(json.dumps({"sketch.md": "0" * 64}), encoding="utf-8")
        report = self.u.verify()
        self.assertFailsAt(report, 4)
        self.assertIn("surface corrupt", report.failed_detail)
        self.assertNotIn("stale", report.failed_detail)

    def test_deleted_store_file_falls_back_to_the_evidence_copy(self):
        self.u.store_file(0).unlink()
        report = self.u.verify()
        self.assertTrue(report.ok, report)

    def test_no_state_directory_uses_the_evidence_copy(self):
        shutil.rmtree(self.u.state)
        report = self.u.verify()
        self.assertTrue(report.ok, report)

    def test_no_state_directory_and_tampered_evidence_copy_is_surface_corrupt(self):
        shutil.rmtree(self.u.state)
        def tamper(lines):
            lines[0]["surface"]["sketch.md"] = "0" * 64
        self.u.rewrite_evidence(tamper)
        report = self.u.verify()
        self.assertFailsAt(report, 4)
        self.assertIn("surface corrupt", report.failed_detail)


class TestLegacy(_Base):

    def test_schema_one_manifest_is_reported_as_legacy(self):
        legacy = {"slug": "u1", "title": "", "created": "2026-01-01T00:00:00",
                  "status": "in_progress", "meta": {}, "gates": {}}
        self.u.write_manifest(legacy)
        report = self.u.verify()
        self.assertTrue(report.legacy)
        self.assertFalse(report.ok)
        self.assertIsNone(report.failed_step)


class TestStep5(_Check):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.u = Unit(self._tmp.name)
        self.u.write("sketch.md", "first sketch\n")
        self.u.seal("composed", mac_key=MAC_KEY)
        self.u.project()

    def test_valid_mac_passes(self):
        report = self.u.verify(mac_key=MAC_KEY)
        self.assertTrue(report.ok, report)
        self.assertEqual(report.steps[4].status, "pass")

    def test_wrong_key_fails_step_5(self):
        report = self.u.verify(mac_key="x" * 32)
        self.assertFailsAt(report, 5)


if __name__ == "__main__":
    unittest.main()
