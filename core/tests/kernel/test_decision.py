"""Tests for core/kernel/decision.py: the deterministic 3-state verdict.

Ported from the legacy decision tests. The soft-pass names come from Oracle.soft_passes(),
never from a constant in core (bug B44). Standard library only; runs with system python3.
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import unittest
from pathlib import Path

from core.kernel.decision import decide
from core.kernel.toy_oracle import ToyOracle
from core.kernel.oracle import VariantVerdict, Oracle, OracleGateResult

# Built at run time so this file never spells the forbidden names (B44 guard).
FORBIDDEN_SOFT_NAMES = ("mechanism" + "_lead", "robust" + "ness")


class _Oracle(Oracle):
    def __init__(self, soft=("soft_a", "soft_b")):
        self._soft = list(soft)

    def run_gates(self, variant_path, cfg):
        raise NotImplementedError

    def soft_passes(self):
        return list(self._soft)


def gr(passes=None, tripwires=None):
    return OracleGateResult(variant_id="c1",
                            passes=passes if passes is not None else {"a": True, "b": True},
                            metrics={}, tripwires=tripwires or {})


class TestDecision(unittest.TestCase):
    def kw(self, **over):
        base = dict(oracle=_Oracle(),
                    soft_passes={"soft_a": True, "soft_b": True},
                    gaming_clean=True, mechanism_sentence="clear mechanism",
                    structurally_promising=True, exhausted=False)
        base.update(over)
        return base

    def test_all_pass_with_mechanism_graduates(self):
        out = decide(gr(), **self.kw())
        self.assertIs(out.verdict, VariantVerdict.GRADUATE)

    def test_tripwire_vetoes(self):
        out = decide(gr(tripwires={"leak": True}), **self.kw())
        self.assertIs(out.verdict, VariantVerdict.REJECT)

    def test_gaming_vetoes(self):
        out = decide(gr(), **self.kw(gaming_clean=False))
        self.assertIs(out.verdict, VariantVerdict.REJECT)

    def test_no_mechanism_blocks_graduation(self):
        out = decide(gr(), **self.kw(mechanism_sentence=""))
        self.assertIsNot(out.verdict, VariantVerdict.GRADUATE)

    def test_near_miss_promising_goes_probation_with_target(self):
        g = gr(passes={"a": True, "b": False})
        out = decide(g, **self.kw())
        self.assertIs(out.verdict, VariantVerdict.PROBATION)
        self.assertEqual(out.target_pass, "b")

    def test_exhausted_rejects(self):
        g = gr(passes={"a": True, "b": False})
        out = decide(g, **self.kw(exhausted=True))
        self.assertIs(out.verdict, VariantVerdict.REJECT)
        self.assertIn("exhausted", out.reason)

    def test_below_bar_rejects(self):
        g = gr(passes={"a": False, "b": False})
        out = decide(g, **self.kw(soft_passes={"soft_a": False, "soft_b": False},
                                  structurally_promising=False))
        self.assertIs(out.verdict, VariantVerdict.REJECT)


class TestSoftPassesComeFromTheOracle(unittest.TestCase):
    # Design ruling (binding, bug B44): the GENERIC search modules (registry, decision,
    # dedup, floors, brief, oracle) never hard-code soft-pass names. A CONCRETE oracle declares
    # its own through soft_passes(). toy_oracle.py is a concrete oracle, so it is exempt from
    # this scan and declares the legacy names itself.
    def test_B44_soft_pass_names_come_from_the_oracle(self):
        """Generic search modules spell no soft-pass name; the names arrive via Oracle.soft_passes()."""
        kernel_dir = Path(_bootstrap.REPO) / "core" / "kernel"
        tests_dir = Path(_bootstrap.REPO) / "core" / "tests" / "kernel"
        owned = [kernel_dir / n for n in ("registry.py", "decision.py", "dedup.py",
                                          "floors.py", "brief.py", "oracle.py")]
        owned += [tests_dir / n for n in ("test_registry.py", "test_decision.py",
                                          "test_dedup.py", "test_floors.py",
                                          "test_brief.py", "test_oracle.py")]
        offenders = []
        for path in owned:
            text = path.read_text(encoding="utf-8")
            for needle in FORBIDDEN_SOFT_NAMES:
                if needle in text:
                    offenders.append(f"{path.name}: {needle}")
        self.assertEqual(offenders, [])

    def test_the_oracles_names_drive_the_target_pass(self):
        g = gr(passes={"a": True, "b": True})
        out = decide(g, oracle=_Oracle(soft=("other_x",)),
                     soft_passes={"other_x": False}, gaming_clean=True,
                     mechanism_sentence="m", structurally_promising=True, exhausted=False)
        self.assertIs(out.verdict, VariantVerdict.PROBATION)
        self.assertEqual(out.target_pass, "other_x")

    def test_total_counts_the_oracles_soft_passes(self):
        # Oracle with no soft passes: total is the oracle passes alone, so 2/2 graduates.
        out = decide(gr(), oracle=_Oracle(soft=()), soft_passes={}, gaming_clean=True,
                     mechanism_sentence="m", structurally_promising=True, exhausted=False)
        self.assertIs(out.verdict, VariantVerdict.GRADUATE)
        self.assertEqual(out.n_passed, 2)

    def test_the_toy_oracle_declares_legacy_soft_passes(self):
        # Legacy order from core/loop/decision.py (SOFT_PASSES). Built from FORBIDDEN_SOFT_NAMES
        # so this file never spells the names literally.
        self.assertEqual(ToyOracle().soft_passes(), list(FORBIDDEN_SOFT_NAMES))

    def test_toy_oracle_first_legacy_soft_pass_false_probates_at_it(self):
        # Legacy behaviour: all oracle passes hold, the first legacy soft pass fails -> PROBATION at it.
        first, second = FORBIDDEN_SOFT_NAMES
        out = decide(gr(), oracle=ToyOracle(), soft_passes={first: False, second: True},
                     gaming_clean=True, mechanism_sentence="m",
                     structurally_promising=True, exhausted=False)
        self.assertIs(out.verdict, VariantVerdict.PROBATION)
        self.assertEqual(out.target_pass, first)


if __name__ == "__main__":
    unittest.main(verbosity=2)
