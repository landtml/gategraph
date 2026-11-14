"""Tests for core/kernel/floors.py: the generic metric-floor threshold layer.

Ported from the legacy gates tests. Standard library only; runs with system python3.
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import unittest

from core.kernel import floors
from core.kernel.oracle import OracleGateResult


def gr(passes=None, metrics=None):
    return OracleGateResult(variant_id="c1",
                            passes=passes if passes is not None else {"a": True, "b": True},
                            metrics=metrics or {})


class TestMetricFloors(unittest.TestCase):
    def test_floor_pass_and_fail(self):
        out = floors.evaluate_metric_floors({"score": 0.9, "cov": 0.5},
                                            {"score": 0.8, "cov": 0.6})
        self.assertEqual(out, {"score": True, "cov": False})

    def test_missing_metric_fails_its_floor(self):
        out = floors.evaluate_metric_floors({}, {"score": 0.8})
        self.assertEqual(out, {"score": False})

    def test_apply_floors_overrides_oracle_pass_for_floored_metric(self):
        g = gr(passes={"score": True, "other": True}, metrics={"score": 0.1})
        g = floors.apply_metric_floors(g, {"score": 0.8})
        self.assertFalse(g.passes["score"])   # floor owns this pass
        self.assertTrue(g.passes["other"])    # oracle-computed pass preserved

    def test_no_floors_is_a_no_op(self):
        g = gr(passes={"a": True})
        self.assertEqual(floors.apply_metric_floors(g, {}).passes, {"a": True})


if __name__ == "__main__":
    unittest.main(verbosity=2)
