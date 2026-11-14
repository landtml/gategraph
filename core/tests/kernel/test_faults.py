"""Tests for core/kernel/faults.py: variant versus infrastructure faults (plan K7;
core/loop/errors.py).

Standard library only. Runs with the system python3 and with .venv/bin/python.

The kernel cannot import core.loop, so the exception classes are matched by name along
the MRO. The classes below use the same names as the loop's classes.
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import unittest

from core.kernel import faults
from core.kernel.types import FaultKind, FaultRoute


class ContractError(Exception):
    """Same name as core/loop/workers.py ContractError."""


class SmokeError(Exception):
    """Same name as core/loop/errors.py SmokeError."""


class RateLimited(Exception):
    """Same name as core/loop/workers.py RateLimited."""


class RateLimitedWithHint(RateLimited):
    retry_after_s = 120.0


class AuthError(Exception):
    """Same name as core/loop/workers.py AuthError (escalates, infrastructure)."""


class BudgetExceeded(Exception):
    """Same name as core/loop/workers.py BudgetExceeded (infrastructure)."""


class VariantSubclass(ContractError):
    """A subclass keeps the variant routing."""


class TestVariantFaults(unittest.TestCase):
    def test_contract_error_is_a_variant_strike(self):
        route = faults.classify(ContractError("output malformed"))
        self.assertIsInstance(route, FaultRoute)
        self.assertEqual(route.fault, FaultKind.VARIANT)
        self.assertTrue(route.strike)
        self.assertFalse(route.retryable)
        self.assertIsNone(route.backoff_s)

    def test_smoke_error_is_a_variant_strike(self):
        route = faults.classify(SmokeError("smoke failed"))
        self.assertEqual(route.fault, FaultKind.VARIANT)
        self.assertTrue(route.strike)

    def test_subclass_of_contract_error_is_variant(self):
        route = faults.classify(VariantSubclass("x"))
        self.assertEqual(route.fault, FaultKind.VARIANT)
        self.assertTrue(route.strike)

    def test_variant_reason_names_the_exception(self):
        self.assertIn("ContractError", faults.classify(ContractError("boom")).reason)


class TestRateLimit(unittest.TestCase):
    def test_rate_limit_is_retryable_with_backoff(self):
        route = faults.classify(RateLimited("window full"))
        self.assertEqual(route.fault, FaultKind.INFRASTRUCTURE)
        self.assertTrue(route.retryable)
        self.assertIsNotNone(route.backoff_s)
        self.assertGreater(route.backoff_s, 0)
        self.assertEqual(route.backoff_s, faults.DEFAULT_RATE_LIMIT_BACKOFF_S)

    def test_rate_limit_never_strikes_the_variant(self):
        route = faults.classify(RateLimited("window full"))
        self.assertFalse(route.strike)

    def test_rate_limit_hint_sets_the_backoff(self):
        route = faults.classify(RateLimitedWithHint("window full"))
        self.assertEqual(route.backoff_s, 120.0)
        self.assertFalse(route.strike)
        self.assertTrue(route.retryable)

    def test_invalid_hint_falls_back_to_default(self):
        class Bad(RateLimited):
            retry_after_s = -5.0
        self.assertEqual(faults.classify(Bad("x")).backoff_s,
                         faults.DEFAULT_RATE_LIMIT_BACKOFF_S)

    def test_rate_limit_wins_over_variant_ancestry(self):
        class RateLimitedContract(ContractError):
            pass
        RateLimitedContract.__name__ = "RateLimited"
        route = faults.classify(RateLimitedContract("x"))
        self.assertEqual(route.fault, FaultKind.INFRASTRUCTURE)
        self.assertFalse(route.strike)


class TestInfrastructureFaults(unittest.TestCase):
    def test_unknown_exceptions_are_infrastructure(self):
        for exc in (RuntimeError("x"), ValueError("x"), OSError("x"), KeyError("k"),
                    Exception("x")):
            with self.subTest(exc=type(exc).__name__):
                route = faults.classify(exc)
                self.assertEqual(route.fault, FaultKind.INFRASTRUCTURE)
                self.assertFalse(route.strike)

    def test_known_loop_infra_classes_are_infrastructure(self):
        for exc in (AuthError("login"), BudgetExceeded("budget")):
            with self.subTest(exc=type(exc).__name__):
                route = faults.classify(exc)
                self.assertEqual(route.fault, FaultKind.INFRASTRUCTURE)
                self.assertFalse(route.strike)
                self.assertFalse(route.retryable)

    def test_rate_limit_phrase_in_plain_text_is_not_a_rate_limit(self):
        # Bug B24 spirit: classification follows the exception type, never the message.
        route = faults.classify(RuntimeError("usage limit reached"))
        self.assertEqual(route.fault, FaultKind.INFRASTRUCTURE)
        self.assertFalse(route.retryable)
        self.assertIsNone(route.backoff_s)

    def test_reason_is_never_empty(self):
        for exc in (ContractError("x"), RateLimited("x"), RuntimeError("x")):
            with self.subTest(exc=type(exc).__name__):
                self.assertTrue(faults.classify(exc).reason)


class TestIsRetryable(unittest.TestCase):
    def test_rate_limit_is_retryable(self):
        self.assertTrue(faults.is_retryable(RateLimited("x")))

    def test_variant_faults_are_not_retryable(self):
        self.assertFalse(faults.is_retryable(ContractError("x")))
        self.assertFalse(faults.is_retryable(SmokeError("x")))

    def test_unknown_is_not_retryable(self):
        self.assertFalse(faults.is_retryable(RuntimeError("x")))
        self.assertFalse(faults.is_retryable(AuthError("x")))


if __name__ == "__main__":
    unittest.main()
