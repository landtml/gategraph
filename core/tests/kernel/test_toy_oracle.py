"""Tests for core/kernel/toy_oracle.py: the data-free reference Oracle.

Standard library only; runs with system python3.
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import tempfile
import unittest
from pathlib import Path

from core.kernel.oracle import Oracle, OracleGateResult
from core.kernel.toy_oracle import ToyOracle


class TestToyOracle(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def variant(self, body, cid="c001"):
        d = self.root / cid
        d.mkdir(parents=True, exist_ok=True)
        path = d / "variant.py"
        path.write_text(body, encoding="utf-8")
        return str(path)

    def test_is_an_oracle_with_a_fixed_incumbent_and_space(self):
        o = ToyOracle()
        self.assertIsInstance(o, Oracle)
        self.assertEqual(o.incumbent(), {"score": 0.5})
        self.assertEqual(o.proposal_space(), ["toy-alpha", "toy-beta", "toy-gamma"])

    def test_default_variant_passes_everything(self):
        g = ToyOracle().run_gates(self.variant("X = 1\n"), {})
        self.assertIsInstance(g, OracleGateResult)
        self.assertEqual(g.variant_id, "c001")
        self.assertEqual(g.passes, {"quality": True, "novelty": True})
        self.assertEqual(g.metrics, {"score": 1.0})
        self.assertTrue(g.tripwire_ok)

    def test_variant_scripts_its_outcome(self):
        body = ("TOY_PASSES = {'novelty': False}\n"
                "TOY_METRICS = {'score': 0.9}\n"
                "TOY_TRIPWIRES = {'leak': True}\n"
                "TOY_NOTES = 'scripted'\n")
        g = ToyOracle().run_gates(self.variant(body), {})
        self.assertEqual(g.passes, {"quality": True, "novelty": False})
        self.assertEqual(g.metrics, {"score": 0.9})
        self.assertFalse(g.tripwire_ok)
        self.assertEqual(g.notes, "scripted")

    def test_a_broken_variant_raises_for_the_driver_to_route(self):
        with self.assertRaises(Exception):
            ToyOracle().run_gates(self.variant("raise RuntimeError('bug')\n"), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
