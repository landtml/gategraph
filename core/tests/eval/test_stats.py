"""Measurement statistics (core/eval/stats.py): Wilson CI, exact McNemar, ICC(1).

Ported from the legacy core/tests/test_eval.py (X1 B1). Offline, stdlib only.
Run standalone: ``python3 core/tests/eval/test_stats.py``.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import unittest

from core.eval import stats


class TestWilsonCI(unittest.TestCase):
    def test_midpoint_symmetric(self):
        lo, hi = stats.wilson_ci(5, 10)
        self.assertAlmostEqual(lo, 0.2366, places=3)
        self.assertAlmostEqual(hi, 0.7634, places=3)

    def test_zero_successes_lower_bound_is_zero(self):
        lo, hi = stats.wilson_ci(0, 10)
        self.assertEqual(lo, 0.0)
        self.assertGreater(hi, 0.0)  # Wilson never claims certainty from n=10

    def test_all_successes_upper_bound_is_one(self):
        lo, hi = stats.wilson_ci(10, 10)
        self.assertEqual(hi, 1.0)
        self.assertLess(lo, 1.0)

    def test_empty_sample_is_maximally_uncertain(self):
        self.assertEqual(stats.wilson_ci(0, 0), (0.0, 1.0))


class TestMcNemarExact(unittest.TestCase):
    # b = pairs where OFF passed / ON failed; c = OFF failed / ON passed.
    def test_six_discordant_one_direction(self):
        self.assertAlmostEqual(stats.mcnemar_exact(0, 6), 0.03125, places=6)

    def test_one_vs_eight(self):
        self.assertAlmostEqual(stats.mcnemar_exact(1, 8), 0.0390625, places=7)

    def test_balanced_discordance_clamps_to_one(self):
        self.assertEqual(stats.mcnemar_exact(3, 3), 1.0)

    def test_no_discordant_pairs_is_uninformative(self):
        self.assertEqual(stats.mcnemar_exact(0, 0), 1.0)

    def test_symmetric_in_arguments(self):
        self.assertEqual(stats.mcnemar_exact(2, 9), stats.mcnemar_exact(9, 2))


class TestICC1(unittest.TestCase):
    def test_perfect_within_case_consistency(self):
        # every case fully agrees with itself across seeds → ICC(1) = 1
        self.assertAlmostEqual(
            stats.icc1([[1, 1, 1], [0, 0, 0], [1, 1, 1]]), 1.0, places=9)

    def test_pure_noise_is_negative(self):
        # identical case means, all variance within-case → ICC(1) = -1 at n=2
        self.assertAlmostEqual(stats.icc1([[1, 0], [0, 1]]), -1.0, places=9)

    def test_degenerate_constant_data_is_one(self):
        # zero variance anywhere = trivially consistent, not a crash
        self.assertEqual(stats.icc1([[1, 1], [1, 1]]), 1.0)

    def test_single_case_is_undefined(self):
        self.assertIsNone(stats.icc1([[1, 0, 1]]))


if __name__ == "__main__":
    unittest.main()
