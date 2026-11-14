"""Tests for core/kernel/registry.py: the append-only variant event log.

Ported from the legacy registry tests. The event contract (latest verdict per variant wins)
is unchanged. Standard library only; runs with system python3.
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import json
import tempfile
import unittest
from dataclasses import asdict, dataclass
from pathlib import Path

from core.kernel.oracle import VariantVerdict
from core.kernel.registry import Registry


@dataclass
class ProposalCard:
    id: str
    hypothesis: str
    mechanism: str
    cell: str
    kill_criteria: str

    def to_dict(self):
        return {"kind": "proposal", **asdict(self)}


@dataclass
class VerdictCard:
    variant_id: str
    verdict: str
    soft_passes: dict
    mechanism_sentence: str = ""
    target_pass: str | None = None
    why: str = ""
    voids_cell: bool = True

    def to_dict(self):
        return {"kind": "verdict", **asdict(self)}


def card(cid="c001", cell="axis-a"):
    return ProposalCard(id=cid, hypothesis="h", mechanism="m", cell=cell, kill_criteria="k")


def reject_card(cid, cell_voids=True, why="dead"):
    return VerdictCard(variant_id=cid, verdict="REJECT", soft_passes={},
                       why=why, voids_cell=cell_voids)


class TmpBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class TestRegistry(TmpBase):
    def reg(self):
        return Registry(self.root / "registry.jsonl")

    def test_record_and_reduce(self):
        r = self.reg()
        r.record(card("c1", cell="axis-a"))
        r.record(reject_card("c1"))
        r.record(card("c2", cell="axis-b"))
        r.record(VerdictCard(variant_id="c2", verdict="GRADUATE", soft_passes={},
                             mechanism_sentence="m"))
        self.assertEqual(r.graduated_ids(), ["c2"])
        self.assertEqual(r.dead_end_cells(), {"axis-a"})

    def test_latest_verdict_per_variant_wins(self):
        r = self.reg()
        r.record(card("c1", cell="axis-a"))
        r.record(reject_card("c1"))
        r.record(VerdictCard(variant_id="c1", verdict="GRADUATE", soft_passes={},
                             mechanism_sentence="m"))
        self.assertEqual(r.graduated_ids(), ["c1"])
        self.assertEqual(r.dead_end_cells(), set())

    def test_tripwire_reject_does_not_burn_cell(self):
        r = self.reg()
        r.record(card("c1", cell="axis-a"))
        r.record(reject_card("c1", cell_voids=False))
        self.assertEqual(r.dead_end_cells(), set())

    def test_torn_final_line_tolerated_interior_raises(self):
        r = self.reg()
        r.record(card("c1"))
        with r.path.open("a", encoding="utf-8") as fh:
            fh.write('{"kind": "torn...')
        self.assertEqual(len(r.events()), 1)  # torn tail dropped
        with r.path.open("w", encoding="utf-8") as fh:
            fh.write('{"bad json\n')
            fh.write(json.dumps(card("c2").to_dict()) + "\n")
        with self.assertRaises(json.JSONDecodeError):
            r.events()

    def test_record_repairs_a_torn_tail_before_appending(self):
        r = self.reg()
        r.record(card("c1"))
        with r.path.open("a", encoding="utf-8") as fh:
            fh.write('{"kind": "torn')
        r.record(card("c2"))
        ids = [e["id"] for e in r.events()]
        self.assertEqual(ids, ["c1", "c2"])

    def test_latest_probation_id(self):
        r = self.reg()
        for cid in ("c1", "c2"):
            r.record(card(cid, cell=f"cell-{cid}"))
            r.record(VerdictCard(variant_id=cid, verdict="PROBATION", soft_passes={},
                                 target_pass="a"))
        self.assertEqual(r.latest_probation_id(), "c2")

    def test_verdicts_are_variant_verdicts(self):
        r = self.reg()
        r.record(card("c1"))
        r.record(reject_card("c1"))
        self.assertIs(r._latest_verdict_by_variant()["c1"], VariantVerdict.REJECT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
