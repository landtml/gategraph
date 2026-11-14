"""Tests for core/kernel/dedup.py: exact dead-end match, then the optional blinded judge.

Ported from the legacy dedup tests. Standard library only; runs with system python3.
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import tempfile
import unittest
from dataclasses import asdict, dataclass
from pathlib import Path

from core.kernel import dedup
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
    why: str = ""
    voids_cell: bool = True

    def to_dict(self):
        return {"kind": "verdict", **asdict(self)}


def card(cid="c001", cell="axis-a"):
    return ProposalCard(id=cid, hypothesis="h", mechanism="m", cell=cell, kill_criteria="k")


def reject_card(cid, why="dead"):
    return VerdictCard(variant_id=cid, verdict="REJECT", soft_passes={}, why=why)


class TmpBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class TestDedup(TmpBase):
    def reg_with_dead(self, cell="axis-a"):
        r = Registry(self.root / "r.jsonl")
        r.record(card("c1", cell=cell))
        r.record(reject_card("c1", why="aha: falsified"))
        return r

    def test_exact_dead_end_match_blocks(self):
        r = self.reg_with_dead("Axis-A")
        res = dedup.dedup_check(card("c2", cell="axis-a"), r)
        self.assertFalse(res.distinct)
        self.assertEqual(res.source, "exact")

    def test_normalize_cell_lowercases_and_trims_parts(self):
        self.assertEqual(dedup.normalize_cell(" Axis / B "), "axis/b")

    def test_no_judge_detariffs_to_registry_key_only(self):
        r = self.reg_with_dead("axis-a")
        res = dedup.dedup_check(card("c2", cell="axis-b"), r)
        self.assertTrue(res.distinct)

    def test_judge_duplicate_high_confidence_blocks(self):
        r = self.reg_with_dead("axis-a")
        res = dedup.dedup_check(
            card("c2", cell="axis-a-renamed"), r,
            judge_fn=lambda c, n: {"decision": "DUPLICATE", "confidence": 0.9,
                                   "reason": "same mechanism"})
        self.assertFalse(res.distinct)
        self.assertEqual(res.source, "judge")

    def test_judge_duplicate_low_confidence_allowed_flagged(self):
        r = self.reg_with_dead("axis-a")
        res = dedup.dedup_check(
            card("c2", cell="axis-a-renamed"), r,
            judge_fn=lambda c, n: {"decision": "DUPLICATE", "confidence": 0.3,
                                   "reason": "maybe"})
        self.assertTrue(res.distinct)
        self.assertTrue(res.flagged)

    def test_nearest_dead_ends_ranks_by_overlap(self):
        r = Registry(self.root / "r.jsonl")
        r.record(ProposalCard(id="c1", hypothesis="momentum decay signal",
                              mechanism="momentum decay in trending regimes",
                              cell="momentum-decay", kill_criteria="k"))
        r.record(reject_card("c1"))
        r.record(ProposalCard(id="c2", hypothesis="unrelated",
                              mechanism="calendar seasonality",
                              cell="calendar", kill_criteria="k"))
        r.record(reject_card("c2"))
        near = dedup.nearest_dead_ends(
            ProposalCard(id="c3", hypothesis="momentum decay revisited",
                         mechanism="decay of momentum", cell="momentum-two",
                         kill_criteria="k"), r, k=1)
        self.assertEqual(near[0]["cell"], "momentum-decay")


if __name__ == "__main__":
    unittest.main(verbosity=2)
