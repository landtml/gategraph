"""Tests for core/kernel/budget.py: the refuse-if-unbounded rule and the spend backstops
(plan 1.14; bug B19).

Standard library only. Runs with the system python3 and with .venv/bin/python.
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import unittest

from core.kernel import budget
from core.kernel.types import Budget, Spend


class TestRefuseUnbounded(unittest.TestCase):
    def test_all_three_backstops_null_is_refused(self):
        with self.assertRaises(budget.UnboundedBudget) as ctx:
            budget.refuse_unbounded(Budget())
        self.assertIn("allow_unbounded", str(ctx.exception))

    def test_all_null_with_explicit_opt_in_is_allowed(self):
        self.assertIsNone(budget.refuse_unbounded(Budget(allow_unbounded=True)))

    def test_cost_bound_alone_is_enough(self):
        budget.refuse_unbounded(Budget(max_usd=1.0))

    def test_iteration_bound_alone_is_enough(self):
        budget.refuse_unbounded(Budget(max_iterations=10))

    def test_wall_clock_bound_alone_is_enough(self):
        budget.refuse_unbounded(Budget(max_wall_s=600.0))

    def test_unbounded_error_is_a_value_error(self):
        self.assertTrue(issubclass(budget.UnboundedBudget, ValueError))


class TestShouldStop(unittest.TestCase):
    def test_nothing_spent_does_not_stop(self):
        decision = budget.should_stop(Spend(), Budget(max_usd=5.0, max_iterations=3,
                                                      max_wall_s=60.0))
        self.assertFalse(decision.stop)
        self.assertEqual(decision.reason, "continue")

    def test_cost_at_limit_stops_with_named_reason(self):
        decision = budget.should_stop(Spend(usd=5.0), Budget(max_usd=5.0))
        self.assertTrue(decision.stop)
        self.assertIn("cost", decision.reason)

    def test_accepts_plain_dict_spend(self):
        decision = budget.should_stop({"usd": 0.0, "iterations": 3}, Budget(max_iterations=3))
        self.assertTrue(decision.stop)
        self.assertIn("iteration", decision.reason)

    def test_missing_dict_keys_count_as_zero(self):
        self.assertFalse(budget.should_stop({}, Budget(max_usd=1.0)).stop)

    def test_unset_bounds_are_ignored(self):
        self.assertFalse(budget.should_stop(Spend(usd=1e9, tokens=10**12),
                                            Budget(max_wall_s=10.0)).stop)

    def test_each_bound_has_its_own_reason(self):
        cases = [
            (Spend(tokens=10), Budget(max_tokens=10), "token"),
            (Spend(model_calls=2), Budget(max_model_calls=2), "model"),
            (Spend(tool_calls=2), Budget(max_tool_calls=2), "tool"),
            (Spend(elapsed_s=60.0), Budget(max_wall_s=60.0), "wall"),
        ]
        for spend, bounds, word in cases:
            with self.subTest(word=word):
                decision = budget.should_stop(spend, bounds)
                self.assertTrue(decision.stop)
                self.assertIn(word, decision.reason)

    def test_B19_elapsed_accumulates_across_restarts(self):
        # Bug B19: the wall-clock backstop was measured from the start of the current
        # process, so a restart reset it. Elapsed time now lives in the checkpointed spend,
        # so a fresh process (no in-memory clock) sees the time already spent.
        bounds = Budget(max_wall_s=80.0)

        # first process: 50 s of work, then checkpoint
        spend = Spend(elapsed_s=50.0)
        checkpoint = spend.to_dict()
        self.assertFalse(budget.should_stop(Spend.from_dict(checkpoint), bounds).stop)

        # restart: a new process proceeds from the checkpoint and adds 40 s
        restored = Spend.from_dict(checkpoint)
        restored = Spend.from_dict({**restored.to_dict(), "elapsed_s": restored.elapsed_s + 40.0})
        decision = budget.should_stop(restored, bounds)
        self.assertTrue(decision.stop)
        self.assertIn("wall", decision.reason)

    def test_B19_restart_does_not_reset_the_clock(self):
        # Same run, but the second process starts from zero elapsed: it must not stop
        # early, which would be the mirror-image bug of ignoring the checkpoint.
        self.assertFalse(budget.should_stop(Spend(elapsed_s=0.0),
                                            Budget(max_wall_s=80.0)).stop)


if __name__ == "__main__":
    unittest.main()
