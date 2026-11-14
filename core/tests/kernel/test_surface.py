"""Surface hashing, the surface store, and effective and stale gates.

The staleness expectations mirror the golden snapshots in tests/golden/expected
(scenario ids are named in each test). Runs on the system python3: standard
library and PyYAML only.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import functools
import json
import os
import tempfile
import unittest
from pathlib import Path

import yaml

from core.kernel.types import Approval, GateSpec, RecordKind, Surface, SurfaceSpec
from core.kernel.types import canonical_json
from core.kernel.evidence import ChainError, make_record, verify_chain
from core.kernel.surface import (SurfaceMissing, effective_gates, hash_surface,
                                 load_surface, stale_gates, store_surface)

REPO = _bootstrap.REPO
GOLDEN = REPO / "tests" / "golden"
HEX_A = "a" * 64
CONFIG = "c" * 64
ACTOR = {"kind": "tool", "id": "test-tool", "channel": None}
# Only migration writes import records (evidence.IMPORT_ACTOR_ID); the guard is tested in
# test_evidence.py.
IMPORT_ACTOR = {"kind": "tool", "id": "migrate", "channel": "legacy"}
UNREVIEWED = SurfaceSpec(include=())          # approval and unhashed gates (legacy)
MIDFLOW = SurfaceSpec(include=("**",), exclude=("review.json",))

# The eval pack's gate chain (tests/golden/scenarios.yaml, legacy adapter):
# scope_confirmed (approval), composed (unhashed), lint_passed, pdf_verified
# (hashed, declared evidence review.json exempt mid-flow), packaged (final, seals).
GATES = [
    GateSpec(id="scope_confirmed", approval=Approval(required=True, channel="tty"),
             surface=UNREVIEWED),
    GateSpec(id="composed", needs=("scope_confirmed",), surface=UNREVIEWED),
    GateSpec(id="lint_passed", needs=("composed",), surface=MIDFLOW),
    GateSpec(id="pdf_verified", needs=("lint_passed",), surface=MIDFLOW),
    GateSpec(id="packaged", needs=("pdf_verified",), seals_unit=True),
]
ALL = ["scope_confirmed", "composed", "lint_passed", "pdf_verified", "packaged"]


def _at(seq):
    return f"2026-01-01T00:00:{seq:02d}+00:00"


class UnitHarness:
    """A unit directory, a state directory and a chain, as the graph's seal node
    would leave them: each seal stores the surface and appends one record."""

    def __init__(self, unit, state, gates=GATES):
        self.unit = Path(unit)
        self.state = Path(state)
        self.gates = gates
        self.chain = []
        self.surfaces = functools.partial(load_surface, self.state)

    def write(self, name, text):
        path = self.unit / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def delete(self, name):
        (self.unit / name).unlink()

    def seal(self, gate, kind=RecordKind.GATE):
        digest = store_surface(self.state, hash_surface(self.unit, gate, self.gates))
        self.chain.append(make_record(
            self.chain, kind=kind, gate=gate, at=_at(len(self.chain)),
            surface_digest=digest, producers=(), config_digest=CONFIG,
            actor=IMPORT_ACTOR if kind == RecordKind.IMPORT else ACTOR))

    def view(self):
        return (effective_gates(self.chain, self.gates, self.unit, self.surfaces),
                stale_gates(self.chain, self.gates, self.unit, self.surfaces))


class _Base(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.h = UnitHarness(root / "unit", root / "state")
        self.h.unit.mkdir()

    def seal_to(self, last):
        kinds = {"scope_confirmed": RecordKind.APPROVAL}
        for gate in ALL[:ALL.index(last) + 1]:
            self.h.seal(gate, kinds.get(gate, RecordKind.GATE))


class TestGoldenMirror(unittest.TestCase):
    """Replays every non-legacy golden scenario that uses only the ops this
    harness models, and compares effective and stale after every step."""

    OPS = {"start", "status", "write", "add", "edit", "delete", "approve",
           "tool_set", "lint", "earn", "pause", "proceed"}
    EARN = [("approve", "scope_confirmed"), ("tool_set", "composed"),
            ("lint", "lint_passed"), ("tool_set", "pdf_verified"),
            ("tool_set", "packaged")]

    def _eligible(self):
        scenarios = yaml.safe_load((GOLDEN / "scenarios.yaml").read_text(encoding="utf-8"))
        for scen in scenarios:
            if scen.get("legacy_bug") or scen.get("adapter"):
                continue
            if any(st.get("unit") not in (None, "u1") for st in scen["steps"]):
                continue
            if not {st["op"] for st in scen["steps"]} <= self.OPS:
                continue
            if "{{" in json.dumps(scen["steps"]):
                continue
            snap = json.loads((GOLDEN / "expected" / f"{scen['id']}.json").read_text(encoding="utf-8"))
            if any(step["exit"] != 0 for step in snap["steps"]):
                continue
            yield scen, snap

    @staticmethod
    def _expand(steps):
        out = []
        for st in steps:
            if st["op"] != "earn":
                out.append(st)
                continue
            for op, gate in TestGoldenMirror.EARN:
                if op == "lint":
                    out.append({"op": "lint", "set_gate": gate})
                else:
                    out.append({"op": op, "gate": gate})
                if gate == st["upto"]:
                    break
        return out

    def test_staleness_matches_golden_snapshots(self):
        ran = []
        for scen, snap in self._eligible():
            with self.subTest(scenario=scen["id"]), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                h = UnitHarness(root / "unit", root / "state")
                h.unit.mkdir()
                steps = self._expand(scen["steps"])
                self.assertEqual(len(steps), len(snap["steps"]))
                for i, (st, want) in enumerate(zip(steps, snap["steps"])):
                    op = st["op"]
                    if op in ("write", "add", "edit"):
                        h.write(st["file"], st["content"])
                    elif op == "delete":
                        h.delete(st["file"])
                    elif op in ("start", "pause", "proceed"):
                        h.write("manifest.yaml", f"status: {op}\n")
                    elif op == "approve":
                        h.seal(st["gate"], RecordKind.APPROVAL)
                    elif op == "tool_set":
                        h.seal(st["gate"])
                    elif op == "lint" and st.get("set_gate"):
                        h.seal(st["set_gate"])
                    effective, stale = h.view()
                    with self.subTest(step=i, op=op):
                        self.assertEqual(effective, want["effective"])
                        self.assertEqual(stale, want["stale"])
                ran.append(scen["id"])
        # Fourteen scenarios: five lifecycle and nine stale-* (see the golden list).
        self.assertEqual(len(ran), 14)


class TestStalenessRepresentative(_Base):
    """Named after the golden scenarios each test mirrors."""

    def test_edit_reopens_gate_and_downstream(self):
        # stale-edit-reopens-downstream-chain
        self.h.write("main.tex", "a\n")
        self.seal_to("packaged")
        self.assertEqual(self.h.view(), (ALL, []))
        self.h.write("main.tex", "b\n")
        self.assertEqual(self.h.view(), (["scope_confirmed", "composed"],
                                         ["lint_passed", "pdf_verified", "packaged"]))

    def test_added_file_reopens_gate(self):
        # stale-added-file-reopens-gate
        self.h.write("main.tex", "a\n")
        self.seal_to("lint_passed")
        self.h.write("summary.tex", "new artifact\n")
        self.assertEqual(self.h.view(), (["scope_confirmed", "composed"], ["lint_passed"]))

    def test_deleted_file_reopens_gate(self):
        # stale-deleted-file-reopens-gate
        self.h.write("main.tex", "a\n")
        self.h.write("summary.tex", "b\n")
        self.seal_to("lint_passed")
        self.h.delete("summary.tex")
        self.assertEqual(self.h.view(), (["scope_confirmed", "composed"], ["lint_passed"]))

    def test_relint_after_edit_recovers(self):
        # stale-relint-after-edit-recovers
        self.h.write("main.tex", "a\n")
        self.seal_to("lint_passed")
        self.h.write("main.tex", "changed\n")
        self.assertEqual(self.h.view()[1], ["lint_passed"])
        self.h.seal("lint_passed")
        self.assertEqual(self.h.view(), (["scope_confirmed", "composed", "lint_passed"], []))

    def test_nested_edit_is_covered(self):
        # stale-nested-file-covered
        self.h.write("sub/deep.tex", "a\n")
        self.seal_to("lint_passed")
        self.h.write("sub/deep.tex", "b\n")
        self.assertEqual(self.h.view(), (["scope_confirmed", "composed"], ["lint_passed"]))

    def test_unhashed_gates_ignore_edits(self):
        # stale-unhashed-gate-ignores-edits
        self.h.write("main.tex", "a\n")
        self.h.seal("scope_confirmed", RecordKind.APPROVAL)
        self.h.seal("composed")
        self.h.write("main.tex", "b\n")
        self.h.write("extra.tex", "c\n")
        self.assertEqual(self.h.view(), (["scope_confirmed", "composed"], []))

    def test_declared_evidence_is_exempt_midflow(self):
        # stale-declared-evidence-exempt-midflow
        self.h.write("main.tex", "a\n")
        self.seal_to("lint_passed")
        self.h.write("review.json", "{}\n")
        self.assertEqual(self.h.view(), (["scope_confirmed", "composed", "lint_passed"], []))

    def test_sealing_gate_covers_whole_unit(self):
        # stale-final-gate-not-exempt: the sealing gate has no exemption.
        self.h.write("main.tex", "a\n")
        self.seal_to("packaged")
        self.h.write("review.json", "{}\n")
        self.assertEqual(self.h.view(), (["scope_confirmed", "composed", "lint_passed",
                                          "pdf_verified"], ["packaged"]))

    def test_bookkeeping_files_are_excluded(self):
        # stale-manifest-file-excluded: manifest.yaml and evidence.jsonl never
        # reopen a gate, so adding evidence.jsonl to a sealed unit is safe.
        self.h.write("main.tex", "a\n")
        self.seal_to("packaged")
        self.h.write("manifest.yaml", "status: paused\n")
        self.h.write("evidence.jsonl", "{}\n")
        self.assertEqual(self.h.view(), (ALL, []))
        self.assertNotIn("evidence.jsonl", hash_surface(self.h.unit, "packaged", GATES).files)
        self.assertNotIn("manifest.yaml", hash_surface(self.h.unit, "packaged", GATES).files)

    def test_sealing_gate_surface_is_the_whole_unit(self):
        self.h.write("main.tex", "a\n")
        self.h.write("sub/deep.tex", "b\n")
        self.h.write("build/out.pdf", "c\n")
        files = hash_surface(self.h.unit, "packaged", GATES).files
        self.assertEqual(sorted(files), ["build/out.pdf", "main.tex", "sub/deep.tex"])


class TestProducesOfLaterGate(_Base):

    def test_produces_of_later_gate_is_excluded_from_earlier_surface(self):
        gates = [
            GateSpec(id="scope_confirmed", approval=Approval(required=True, channel="tty"),
                     surface=UNREVIEWED),
            GateSpec(id="lint_passed", needs=("scope_confirmed",)),
            GateSpec(id="packaged", needs=("lint_passed",), produces=("build/**",),
                     seals_unit=True),
        ]
        self.h.gates = gates
        self.h.write("main.tex", "a\n")
        self.h.seal("scope_confirmed", RecordKind.APPROVAL)
        self.h.seal("lint_passed")
        self.h.write("build/out.pdf", "rendered\n")
        self.assertEqual(sorted(hash_surface(self.h.unit, "lint_passed", gates).files),
                         ["main.tex"])
        self.assertIn("build/out.pdf", hash_surface(self.h.unit, "packaged", gates).files)
        # The build output of a later gate does not reopen the earlier gate.
        self.assertEqual(self.h.view(), (["scope_confirmed", "lint_passed"], []))


class TestSurfaceStore(_Base):

    def test_store_writes_once_and_returns_digest(self):
        self.h.write("main.tex", "a\n")
        surface = hash_surface(self.h.unit, "packaged", GATES)
        digest = store_surface(self.h.state, surface)
        self.assertEqual(digest, surface.digest())
        path = self.h.state / "surfaces" / f"{digest}.json"
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_text(encoding="utf-8"), canonical_json(surface.files))

        stamp = path.stat().st_mtime_ns
        names = sorted(os.listdir(self.h.state / "surfaces"))
        self.assertEqual(store_surface(self.h.state, surface), digest)
        self.assertEqual(path.stat().st_mtime_ns, stamp)
        self.assertEqual(sorted(os.listdir(self.h.state / "surfaces")), names)

    def test_store_never_overwrites(self):
        surface = Surface(files={"main.tex": HEX_A})
        digest = store_surface(self.h.state, surface)
        path = self.h.state / "surfaces" / f"{digest}.json"
        path.write_text("tampered", encoding="utf-8")
        self.assertEqual(store_surface(self.h.state, surface), digest)
        self.assertEqual(path.read_text(encoding="utf-8"), "tampered")

    def test_load_round_trips(self):
        self.h.write("main.tex", "a\n")
        surface = hash_surface(self.h.unit, "packaged", GATES)
        digest = store_surface(self.h.state, surface)
        self.assertEqual(load_surface(self.h.state, digest), surface.files)

    def test_load_missing_raises(self):
        with self.assertRaises(SurfaceMissing):
            load_surface(self.h.state, HEX_A)

    def test_load_mismatched_content_raises(self):
        digest = store_surface(self.h.state, Surface(files={"main.tex": HEX_A}))
        path = self.h.state / "surfaces" / f"{digest}.json"
        path.write_text(canonical_json({"main.tex": "b" * 64}), encoding="utf-8")
        with self.assertRaises(SurfaceMissing):
            load_surface(self.h.state, digest)

    def test_load_rejects_a_name_that_is_not_a_digest(self):
        with self.assertRaises(SurfaceMissing):
            load_surface(self.h.state, "../../etc/passwd")

    def test_store_rejects_paths_outside_the_unit(self):
        with self.assertRaises(ValueError):
            store_surface(self.h.state, Surface(files={"../outside.tex": HEX_A}))


class TestB33PathContainment(_Base):
    """B33: recorded file names were joined to the unit path with no check."""

    def _chain_with_map(self, files):
        surface_dir = self.h.state / "surfaces"
        surface_dir.mkdir(parents=True, exist_ok=True)
        digest = Surface(files=files).digest()
        (surface_dir / f"{digest}.json").write_text(canonical_json(files), encoding="utf-8")
        return [make_record([], kind=RecordKind.APPROVAL, gate="scope_confirmed",
                            at=_at(0), surface_digest=digest, producers=(),
                            config_digest=CONFIG, actor=ACTOR)]

    def test_B33_path_outside_unit_rejected(self):
        for name in ("../outside.txt", "/etc/passwd", "sub/../../x", "..\\x", "", "a/./b"):
            with self.subTest(name=name):
                chain = self._chain_with_map({name: HEX_A})
                with self.assertRaises(ChainError):
                    effective_gates(chain, GATES, self.h.unit, self.h.surfaces)

    def test_symlink_escaping_the_unit_is_rejected(self):
        outside = Path(self._tmp.name) / "outside.txt"
        outside.write_text("secret\n", encoding="utf-8")
        os.symlink(outside, self.h.unit / "main.tex")
        with self.assertRaises(ChainError):
            hash_surface(self.h.unit, "packaged", GATES)

    def test_recorded_map_must_match_the_current_unit(self):
        # A valid map with a bad digest value is refused, not treated as stale.
        chain = self._chain_with_map({"main.tex": "not-a-digest"})
        with self.assertRaises(ChainError):
            effective_gates(chain, GATES, self.h.unit, self.h.surfaces)


class TestReseal(_Base):
    """Re-earning a gate invalidates every gate that needs it, directly or
    transitively, when that gate was earned before the re-seal (legacy set_gate
    popped later gates; reset_invalidates_later in eval/corpus/machinery.yaml)."""

    CHAIN3 = [GateSpec(id="g1"), GateSpec(id="g2", needs=("g1",)),
              GateSpec(id="g3", needs=("g2",))]

    def test_reseal_invalidates_later_gates(self):
        self.h.gates = self.CHAIN3
        self.h.write("main.tex", "a\n")
        for gate in ("g1", "g2", "g3"):
            self.h.seal(gate)
        self.assertEqual(self.h.view(), (["g1", "g2", "g3"], []))
        self.h.seal("g1")                       # re-earn with files unchanged
        effective, _ = self.h.view()
        self.assertEqual(effective, ["g1"])
        self.assertNotIn("g2", effective)
        self.assertNotIn("g3", effective)

    def test_reseal_of_last_gate_keeps_earlier(self):
        self.h.gates = self.CHAIN3
        self.h.write("main.tex", "a\n")
        for gate in ("g1", "g2", "g3"):
            self.h.seal(gate)
        self.h.seal("g3")                       # nothing needs g3, so nothing drops
        self.assertEqual(self.h.view(), (["g1", "g2", "g3"], []))


    def test_import_record_is_effective_until_stale(self):
        # R1: an import record (written only by migration) is a passing record, with the
        # same staleness rule as a gate record: effective while its surface matches disk
        # and its needs are effective, stale as soon as a surface file changes.
        self.h.write("main.tex", "a\n")
        self.h.seal("scope_confirmed", RecordKind.IMPORT)
        self.h.seal("composed", RecordKind.IMPORT)
        self.h.seal("lint_passed", RecordKind.IMPORT)
        self.assertEqual(self.h.view(), (["scope_confirmed", "composed", "lint_passed"], []))
        self.h.write("main.tex", "b\n")
        self.assertEqual(self.h.view(), (["scope_confirmed", "composed"], ["lint_passed"]))


if __name__ == "__main__":
    unittest.main()
