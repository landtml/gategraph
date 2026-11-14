"""Tests for core/kernel/brief.py: the deterministic dead-ends digest.

Ported from the legacy brief tests. Standard library only; runs with system python3.
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import tempfile
import unittest
from dataclasses import asdict, dataclass
from pathlib import Path

from core.kernel import brief
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


def card(cid, cell="axis-a"):
    return ProposalCard(id=cid, hypothesis="h", mechanism="m", cell=cell, kill_criteria="k")


def reject_card(cid):
    return VerdictCard(variant_id=cid, verdict="REJECT", soft_passes={})


class TmpBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class TestBrief(TmpBase):
    def test_digest_lists_dead_cells_and_never_raises(self):
        r = Registry(self.root / "r.jsonl")
        self.assertIn("none recorded", brief.dead_ends_digest(r))
        r.record(card("c1", cell="axis-a"))
        r.record(reject_card("c1"))
        self.assertIn("axis-a", brief.dead_ends_digest(r))

    def test_word_cap(self):
        r = Registry(self.root / "r.jsonl")
        for i in range(200):
            r.record(card(f"c{i}", cell=f"cell-{i}-long-name"))
            r.record(reject_card(f"c{i}"))
        text = brief.dead_ends_digest(r, max_words=50)
        self.assertLessEqual(len(text.split()), 51)

    def test_llm_refinement_is_used_when_it_returns_text(self):
        r = Registry(self.root / "r.jsonl")
        self.assertEqual(brief.build_brief(r, llm_fn=lambda material: "  refined  "),
                         "refined")

    def test_llm_refinement_falls_back_on_failure(self):
        r = Registry(self.root / "r.jsonl")
        fn = brief.make_brief_fn(r, llm_fn=lambda material: (_ for _ in ()).throw(
            RuntimeError("boom")))
        self.assertIn("DEAD-ENDS", fn())


if __name__ == "__main__":
    unittest.main(verbosity=2)
