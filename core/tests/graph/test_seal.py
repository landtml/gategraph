"""G2: the seal node (plan sections 1.5, 1.8; bug B18).

Calls the node as a plain function with a state dict. Seal is the only node
that appends evidence. It re-hashes the surface, refuses on any difference from
the digest taken before the checks, refuses a chain that is already broken, and
stores the surface map through the surface store so the record carries only
its digest. It never calls interrupt and never records a checkpoint id.
Runs on the pinned venv; the node itself imports only core.kernel.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import copy
import inspect
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.graph.contracts import WRITES
from core.graph.nodes import seal as seal_mod
from core.graph.nodes.seal import seal
from core.kernel.evidence import verify_chain
from core.kernel.surface import hash_surface, load_surface
from core.kernel.types import Approval, GateSpec, RecordKind, SurfaceSpec

AT = "2026-11-13T10:00:00+00:00"
CONFIG = "c" * 64
GATES = (
    GateSpec(id="composed"),
    GateSpec(id="reviewed", needs=("composed",)),
)
CHECK = {"id": "policy", "exit": 0, "status": "pass", "findings": [],
         "duration_s": 0.1, "digest": "d" * 64}


class SealTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.unit = root / "unit"
        self.unit.mkdir()
        (self.unit / "a.md").write_text("alpha\n", encoding="utf-8")
        self.state_dir = root / "state"
        self.state_dir.mkdir()
        self.cfg = SimpleNamespace(gates=GATES, config_digest=CONFIG,
                                   unit_dir=self.unit, state_dir=self.state_dir,
                                   clock=lambda: AT)

    def tearDown(self):
        self._tmp.cleanup()

    def state(self, gate="composed", **extra) -> dict:
        """State as it is after the checks ran: the surface digest was taken then."""
        digest = hash_surface(self.unit, gate, GATES).digest()
        base = {"project": "p", "unit": "u1", "actor": "native",
                "config_digest": CONFIG, "evidence": [], "status": "in_progress",
                "effective": [], "stale": [], "target_gate": gate,
                "surface_digest": digest, "check_results": [CHECK], "verdicts": [],
                "findings": [], "attempts": {}, "fail_streak": 0, "spend": {},
                "decisions": []}
        base.update(extra)
        return base


class TestSealAppend(SealTestBase):
    def test_appends_exactly_one_record_with_empty_chain(self):
        update = seal(self.state(), self.cfg)
        self.assertEqual(len(update["evidence"]), 1)
        rec = update["evidence"][0]
        self.assertEqual(rec["seq"], 0)
        self.assertEqual(rec["prev"], "")
        self.assertEqual(rec["gate"], "composed")
        self.assertEqual(rec["kind"], RecordKind.GATE.value)
        self.assertEqual(verify_chain(update["evidence"]), rec["digest"])

    def test_continues_an_existing_chain_from_its_head(self):
        first = seal(self.state(), self.cfg)["evidence"]
        state = self.state(evidence=first)
        update = seal(state, self.cfg)
        rec = update["evidence"][0]
        self.assertEqual(rec["seq"], 1)
        self.assertEqual(rec["prev"], first[0]["digest"])
        self.assertEqual(verify_chain(first + update["evidence"]), rec["digest"])

    def test_writes_only_keys_the_contract_allows(self):
        update = seal(self.state(), self.cfg)
        self.assertTrue(set(update) <= WRITES["seal"], set(update))
        self.assertIsNone(update.get("target_gate"))

    def test_record_carries_only_the_surface_digest(self):
        update = seal(self.state(), self.cfg)
        rec = update["evidence"][0]
        self.assertNotIn("surface", rec)
        self.assertEqual(rec["surface_digest"], hash_surface(self.unit, "composed", GATES).digest())

    def test_surface_map_is_stored_through_the_surface_store(self):
        update = seal(self.state(), self.cfg)
        digest = update["evidence"][0]["surface_digest"]
        self.assertEqual(load_surface(self.state_dir, digest),
                         hash_surface(self.unit, "composed", GATES).files)

    def test_record_has_no_checkpoint_identifier(self):
        state = self.state(checkpoint_id="cp-1")
        update = seal(state, self.cfg)
        self.assertFalse(any("checkpoint" in key for key in update["evidence"][0]))

    def test_producers_come_from_the_attempt_results(self):
        update = seal(self.state(), self.cfg)
        producers = update["evidence"][0]["producers"]
        self.assertEqual([p["id"] for p in producers], ["policy"])
        self.assertEqual(producers[0]["kind"], "check")
        self.assertEqual(producers[0]["digest"], "d" * 64)


    def test_seal_never_writes_an_import_record(self):
        # R1: import records are written only by migration (evidence.IMPORT_ACTOR_ID).
        source = inspect.getsource(seal_mod)
        self.assertNotIn("RecordKind.IMPORT", source)
        self.assertNotIn('"import"', source)
        rec = seal(self.state(), self.cfg)["evidence"][0]
        self.assertIn(rec["kind"], {RecordKind.GATE.value, RecordKind.APPROVAL.value})


class TestSealRefusals(SealTestBase):
    def test_B18_seal_refuses_if_surface_changed_since_checks(self):
        state = self.state()
        (self.unit / "a.md").write_text("edited after the checks\n", encoding="utf-8")
        update = seal(state, self.cfg)
        self.assertFalse(update.get("evidence"))
        self.assertEqual(len(update["findings"]), 1)
        self.assertEqual(update["findings"][0]["rule"], "seal.surface_changed")

    def test_refuses_when_a_file_is_added_to_the_surface(self):
        state = self.state()
        (self.unit / "b.md").write_text("new\n", encoding="utf-8")
        update = seal(state, self.cfg)
        self.assertFalse(update.get("evidence"))
        self.assertEqual(update["findings"][0]["rule"], "seal.surface_changed")

    def test_refuses_a_chain_that_is_already_broken(self):
        first = seal(self.state(), self.cfg)["evidence"]
        broken = copy.deepcopy(first)
        broken[0]["at"] = "2030-01-01T00:00:00+00:00"   # digest no longer matches
        update = seal(self.state(evidence=broken), self.cfg)
        self.assertFalse(update.get("evidence"))
        self.assertEqual(update["findings"][0]["rule"], "seal.chain_broken")

    def test_refuses_when_there_is_no_target_gate(self):
        update = seal(self.state(target_gate=None), self.cfg)
        self.assertFalse(update.get("evidence"))
        self.assertEqual(update["findings"][0]["rule"], "seal.no_target")

    def test_every_refusal_returns_one_fatal_finding_and_only_contract_keys(self):
        first = seal(self.state(), self.cfg)["evidence"]
        broken = copy.deepcopy(first)
        broken[0]["at"] = "2030-01-01T00:00:00+00:00"
        def check(rule, state):
            with self.subTest(rule=rule):
                update = seal(state, self.cfg)
                self.assertTrue(set(update) <= WRITES["seal"], set(update))
                self.assertNotIn("evidence", update)
                self.assertEqual([f["rule"] for f in update["findings"]], [rule])
                self.assertEqual(update["findings"][0]["severity"], "fatal")
                self.assertNotIn("target_gate", update)   # target stays on refusal

        check("seal.no_target", self.state(target_gate=None))
        check("seal.unknown_gate", self.state(target_gate="nope"))
        check("seal.chain_broken", self.state(evidence=broken))
        changed = self.state()                      # digest taken before the edit
        (self.unit / "a.md").write_text("edited\n", encoding="utf-8")
        check("seal.surface_changed", changed)
        # Surface unreadable: a symlink that resolves outside the unit makes hash_surface raise
        # ChainError. Created last so the cases above never see it.
        unreadable = self.state()                   # hashed before the symlink exists
        unreadable["surface_digest"] = "e" * 64     # any digest; hashing now raises
        outside = self.state_dir.parent / "outside.md"
        outside.write_text("x\n", encoding="utf-8")
        (self.unit / "link.md").symlink_to(outside)
        check("seal.surface_unreadable", unreadable)


class TestSealEmptySurface(SealTestBase):
    """Regression (legacy 0.4 core/lint.py, the --set-gate branch, comment F36): a gate
    earned over ZERO files is vacuous, so it is refused. Approval-only gates and unhashed
    gates (surface include=()) have an empty surface by design and are not refused."""

    def setUp(self):
        super().setUp()
        (self.unit / "a.md").unlink()   # the unit now holds no files

    def _seal(self, gates, gate="composed"):
        cfg = SimpleNamespace(gates=gates, config_digest=CONFIG, unit_dir=self.unit,
                              state_dir=self.state_dir, clock=lambda: AT)
        digest = hash_surface(self.unit, gate, gates).digest()
        return seal(self.state(gate=gate, surface_digest=digest), cfg)

    def test_hashed_gate_over_zero_files_is_refused(self):
        update = self._seal(GATES)
        self.assertFalse(update.get("evidence"))
        self.assertEqual([f["rule"] for f in update["findings"]], ["seal.empty_surface"])
        self.assertEqual(update["findings"][0]["severity"], "fatal")
        self.assertNotIn("target_gate", update)

    def test_include_glob_matching_no_file_is_refused(self):
        gates = (GateSpec(id="composed", surface=SurfaceSpec(include=("*.py",))),)
        update = self._seal(gates)
        self.assertEqual([f["rule"] for f in update["findings"]], ["seal.empty_surface"])

    def test_approval_only_gate_over_zero_files_is_sealed(self):
        gates = (GateSpec(id="composed", approval=Approval(required=True, channel="tty")),)
        update = self._seal(gates)
        self.assertEqual(len(update["evidence"]), 1)
        self.assertEqual(update["evidence"][0]["kind"], RecordKind.APPROVAL.value)

    def test_unhashed_gate_over_zero_files_is_sealed(self):
        gates = (GateSpec(id="composed", surface=SurfaceSpec(include=())),)
        update = self._seal(gates)
        self.assertEqual(len(update["evidence"]), 1)
        self.assertNotIn("findings", update)


class TestSealIdempotent(SealTestBase):
    def test_rerun_on_the_same_state_gives_the_same_record_and_digest(self):
        state = self.state()
        first = seal(state, self.cfg)
        second = seal(copy.deepcopy(state), self.cfg)
        self.assertEqual(first, second)
        self.assertEqual(first["evidence"][0]["surface_digest"],
                         second["evidence"][0]["surface_digest"])

    def test_rerun_stores_the_surface_once(self):
        state = self.state()
        seal(state, self.cfg)
        seal(state, self.cfg)
        self.assertEqual(len(list((self.state_dir / "surfaces").glob("*.json"))), 1)


class TestSealSource(unittest.TestCase):
    def test_never_calls_interrupt_and_imports_no_langgraph(self):
        source = inspect.getsource(seal_mod)
        self.assertNotIn("interrupt", source)
        self.assertNotIn("langgraph", source)


if __name__ == "__main__":
    unittest.main()
