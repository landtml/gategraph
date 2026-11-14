"""Projections: atomic, byte-stable evidence.jsonl and manifest.yaml (plan 1.8).

Runs on the system python3 (kernel rules): standard library and PyYAML only.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import functools
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from core.kernel import paths
from core.kernel.config import Config
from core.kernel.evidence import make_record, verify_chain
from core.kernel.projection import ManifestError, read_manifest, write_projections
from core.kernel.surface import hash_surface, load_surface, store_surface
from core.kernel.types import GateSpec, SurfaceSpec

CONFIG = "c" * 64
ACTOR = {"kind": "tool", "id": "test-tool", "channel": None}
GATES = (
    GateSpec(id="composed", surface=SurfaceSpec(include=("sketch.md",))),
    GateSpec(id="lint", needs=("composed",), surface=SurfaceSpec(include=("**",))),
    GateSpec(id="packaged", needs=("lint",), seals_unit=True),
)


def _at(seq):
    return f"2026-01-01T00:00:{seq:02d}+00:00"


class Unit:
    """A unit directory, its state directory and its chain, as the seal node leaves them."""

    def __init__(self, root, name="u1"):
        self.root = Path(root)
        self.unit = self.root / "units" / name
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

    def seal(self, gate):
        digest = store_surface(self.state, hash_surface(self.unit, gate, GATES))
        self.chain.append(make_record(
            self.chain, kind="gate", gate=gate, at=_at(len(self.chain)),
            surface_digest=digest, producers=(), config_digest=CONFIG, actor=ACTOR))

    def project(self, status="in_progress", checkpoint_id="ckpt-1", unit_dir=None):
        return write_projections(unit_dir or self.unit, self.cfg, self.chain,
                                 thread_id="demo/u1", status=status,
                                 surfaces=self.surfaces, config_digest=CONFIG,
                                 checkpoint_id=checkpoint_id)


class _Base(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.u = Unit(self._tmp.name)
        self.u.write("sketch.md", "first sketch\n")
        self.u.seal("composed")
        self.u.write("lint.txt", "clean\n")
        self.u.seal("lint")
        self.u.seal("packaged")


class TestWrite(_Base):

    def test_writes_evidence_and_manifest(self):
        paths_out = self.u.project()
        evidence = Path(paths_out["evidence"]).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(evidence), len(self.u.chain))
        self.assertTrue(Path(paths_out["manifest"]).is_file())
        self.assertEqual(read_manifest(self.u.unit).kind, "current")

    def test_evidence_lines_carry_surface_outside_the_digest(self):
        self.u.project()
        lines = [json.loads(line) for line in
                 (self.u.unit / "evidence.jsonl").read_text(encoding="utf-8").splitlines()]
        for line, rec in zip(lines, self.u.chain):
            self.assertEqual(line["surface"], load_surface(self.u.state, rec.surface_digest))
        stripped = [{k: v for k, v in line.items() if k != "surface"} for line in lines]
        self.assertEqual(verify_chain(stripped), self.u.chain[-1].digest)

    def test_manifest_names_head_gate_table_and_checkpoint(self):
        self.u.project(status="in_progress", checkpoint_id="ckpt-7")
        data = yaml.safe_load((self.u.unit / "manifest.yaml").read_text(encoding="utf-8"))
        self.assertEqual(data["schema"], 2)
        self.assertEqual(data["thread_id"], "demo/u1")
        self.assertEqual(data["checkpoint_id"], "ckpt-7")
        self.assertEqual(data["head"], {"seq": 2, "digest": self.u.chain[-1].digest})
        self.assertEqual(sorted(data["gates"]), ["composed", "lint", "packaged"])
        self.assertEqual(data["gates"]["lint"]["seq"], 1)
        self.assertEqual(data["gates"]["lint"]["digest"], self.u.chain[1].digest)
        self.assertEqual(data["effective"], ["composed", "lint", "packaged"])
        self.assertEqual(data["stale"], [])

    def test_empty_chain_has_no_head(self):
        empty = Unit(self._tmp.name, name="empty")
        empty.project()
        self.assertEqual((empty.unit / "evidence.jsonl").read_text(encoding="utf-8"), "")
        data = yaml.safe_load((empty.unit / "manifest.yaml").read_text(encoding="utf-8"))
        self.assertIsNone(data["head"])
        self.assertEqual(data["gates"], {})


class TestByteStable(_Base):

    def test_same_chain_same_bytes_on_rewrite(self):
        self.u.project()
        first = {name: (self.u.unit / name).read_bytes()
                 for name in ("evidence.jsonl", "manifest.yaml")}
        self.u.project()
        second = {name: (self.u.unit / name).read_bytes()
                  for name in ("evidence.jsonl", "manifest.yaml")}
        self.assertEqual(first, second)

    def test_same_chain_same_bytes_in_another_project(self):
        # A clone: other project root, same files, same chain, copied surface store.
        with tempfile.TemporaryDirectory() as other_root:
            clone = Unit(other_root)
            for name in ("sketch.md", "lint.txt"):
                (clone.unit / name).write_bytes((self.u.unit / name).read_bytes())
            clone.state.mkdir(parents=True)
            (clone.state / "surfaces").symlink_to(self.u.state / "surfaces")
            clone.chain = list(self.u.chain)
            self.u.project()
            mine = {name: (self.u.unit / name).read_bytes()
                    for name in ("evidence.jsonl", "manifest.yaml")}
            clone.project()
            for name, data in mine.items():
                self.assertEqual((clone.unit / name).read_bytes(), data)


class TestAtomic(_Base):

    def test_failed_replace_keeps_the_previous_projection(self):
        self.u.project()
        before = {name: (self.u.unit / name).read_bytes()
                  for name in ("evidence.jsonl", "manifest.yaml")}
        self.u.write("lint.txt", "changed\n")
        self.u.seal("lint")  # a new record: the projection would change
        with mock.patch("core.kernel.paths.os.replace",
                        side_effect=OSError("disk gone")):
            with self.assertRaises(OSError):
                self.u.project()
        for name, text in before.items():
            self.assertEqual((self.u.unit / name).read_bytes(), text)
        leftovers = [p.name for p in self.u.unit.iterdir() if p.name.startswith(".")]
        self.assertEqual(leftovers, [])


class TestReadManifest(_Base):

    def test_missing_manifest_raises(self):
        with self.assertRaises(ManifestError):
            read_manifest(self.u.unit)

    def test_schema_one_manifest_is_a_legacy_projection(self):
        legacy = {
            "slug": "u1", "title": "", "created": "2026-01-01T00:00:00",
            "status": "in_progress", "meta": {},
            "gates": {"composed": {"at": "2026-01-01T00:00:00",
                                  "files": {"sketch.md": "a" * 64}}},
        }
        (self.u.unit / "manifest.yaml").write_text(
            yaml.safe_dump(legacy, sort_keys=False), encoding="utf-8")
        view = read_manifest(self.u.unit)
        self.assertEqual(view.kind, "legacy")
        self.assertEqual(view.data["slug"], "u1")

    def test_corrupt_manifest_raises(self):
        (self.u.unit / "manifest.yaml").write_text("- just\n- a list\n", encoding="utf-8")
        with self.assertRaises(ManifestError):
            read_manifest(self.u.unit)


if __name__ == "__main__":
    unittest.main()
