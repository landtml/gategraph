"""Tests for core/kernel/oracle.py: the Oracle contract, OracleGateResult, VariantVerdict.

Ported from the legacy oracle and schema tests. Standard library only; runs with system python3.
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import subprocess
import tempfile
import unittest
from pathlib import Path

from core.kernel.oracle import (VariantVerdict, Oracle, OracleGateResult,
                                OracleLoadError, load_oracle)


def gr(passes=None, tripwires=None, metrics=None):
    return OracleGateResult(variant_id="c1",
                            passes=passes if passes is not None else {"a": True, "b": True},
                            metrics=metrics or {},
                            tripwires=tripwires or {})


class _NoopOracle(Oracle):
    def run_gates(self, variant_path, cfg):
        return gr()


class TestOracleGateResult(unittest.TestCase):
    def test_roundtrip_keeps_generic_shape(self):
        g = OracleGateResult(variant_id="c1", passes={"p": True}, metrics={"m": 0.5},
                             tripwires={"leak": False}, notes="n")
        d = g.to_dict()
        self.assertEqual(d["kind"], "gate_result")
        g2 = OracleGateResult.from_dict(d)
        self.assertEqual(g2.passes, {"p": True})
        self.assertEqual(g2.tripwires, {"leak": False})
        self.assertEqual(g2.notes, "n")

    def test_tripwire_ok_semantics(self):
        self.assertTrue(gr(tripwires={}).tripwire_ok)
        self.assertTrue(gr(tripwires={"leak": False}).tripwire_ok)
        self.assertFalse(gr(tripwires={"leak": True, "other": False}).tripwire_ok)

    def test_n_passed(self):
        self.assertEqual(gr(passes={"a": True, "b": False, "c": True}).n_passed, 2)

    def test_from_dict_tolerates_missing_optional_fields(self):
        g = OracleGateResult.from_dict({"variant_id": "c", "passes": {}, "metrics": {}})
        self.assertEqual(g.tripwires, {})
        self.assertEqual(g.notes, "")


class TestVariantVerdict(unittest.TestCase):
    def test_values_are_the_search_verdicts(self):
        self.assertEqual({v.value for v in VariantVerdict},
                         {"GRADUATE", "PROBATION", "REJECT"})
        self.assertIs(VariantVerdict("REJECT"), VariantVerdict.REJECT)


class TestOracleContract(unittest.TestCase):
    def test_run_gates_is_the_only_required_method(self):
        with self.assertRaises(TypeError):
            Oracle()  # abstract: run_gates missing
        o = _NoopOracle()
        self.assertIsNone(o.incumbent())
        self.assertEqual(o.proposal_space(), [])
        self.assertEqual(o.soft_passes(), [])

    def test_soft_passes_is_a_list_of_names(self):
        class Two(_NoopOracle):
            def soft_passes(self):
                return ["soft_a", "soft_b"]
        self.assertEqual(Two().soft_passes(), ["soft_a", "soft_b"])


class TestLoadOracle(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_file_spec_resolves_under_root(self):
        (self.root / "pack").mkdir()
        (self.root / "pack" / "oracle.py").write_text(
            "from core.kernel.oracle import Oracle, OracleGateResult\n"
            "class Mine(Oracle):\n"
            "    def run_gates(self, variant_path, cfg):\n"
            "        return OracleGateResult(variant_id='x', passes={}, metrics={})\n",
            encoding="utf-8")
        inst = load_oracle("pack.oracle:Mine", self.root)
        self.assertIsInstance(inst, Oracle)

    def test_bad_spec_and_missing_class_raise(self):
        with self.assertRaises(OracleLoadError):
            load_oracle("no-colon-here", self.root)
        with self.assertRaises(OracleLoadError):
            load_oracle("missing.module:Nope", self.root)

    def test_kernel_spec_resolves_the_shipped_toy_oracle(self):
        inst = load_oracle("core.kernel.toy_oracle:ToyOracle", self.root)
        self.assertIsInstance(inst, Oracle)


class TestNoLoopImport(unittest.TestCase):
    def test_importing_the_kernel_oracle_does_not_load_the_legacy_loop(self):
        repo = _bootstrap.REPO
        code = ("import sys; import core.kernel.oracle, core.kernel.toy_oracle; "
                "print('core.loop' in sys.modules or any("
                "m.startswith('core.loop.') for m in sys.modules))")
        out = subprocess.run([sys.executable, "-c", code], cwd=str(repo),
                             capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "False")


if __name__ == "__main__":
    unittest.main(verbosity=2)
