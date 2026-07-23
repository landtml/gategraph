"""F3+F6: the reference assets must ship the patterns that prevent the
observed consumer bugs.

F3 (consumer-D): a graduated variant violated the WRITTEN contract because the
oracle's sampling grid never crossed the documented boundary. The seed oracle
template must ship an explicit CONTRACT block consumed by run_gates (domain +
boundary probes), so the enforced spec and the narrated spec cannot drift.

F6 (consumer-C): claim checks verified only the claims that PARSED, a
typo'd or deleted claim line silently skipped verification and still earned
the gate. The reference check must take a REQUIRED-claims set and fail on
absence.

Template-side assets, so these tests live in tests/ (never stamped).
"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parent.parent
ORACLE_TMPL = TEMPLATE / "seed" / "oracle.py.tmpl"
VERIFY_CLAIMS = TEMPLATE / "seed" / "checks" / "verify_claims.py"


class TestOracleContractBlock(unittest.TestCase):
    def setUp(self):
        self.text = ORACLE_TMPL.read_text(encoding="utf-8")

    def test_ships_an_explicit_contract_block(self):
        self.assertIn("CONTRACT", self.text,
                      "seed oracle must declare its evaluation contract "
                      "explicitly (F3)")
        for key in ("domain", "boundary_probes"):
            self.assertIn(key, self.text,
                          f"CONTRACT must declare {key!r}, the grid must "
                          f"probe the documented edges")

    def test_run_gates_is_documented_as_the_only_required_method(self):
        self.assertNotIn("three methods", self.text,
                         "stale claim: incumbent/proposal_space are optional "
                         "since the F2/F30 batch")
        self.assertIn("only method", self.text.lower())


class TestRequiredClaimsPattern(unittest.TestCase):
    """Drive seed/checks/verify_claims.py exactly as an adapter check would."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.memo = Path(self._tmp.name) / "memo.md"

    def tearDown(self):
        self._tmp.cleanup()

    def run_check(self, *args):
        return subprocess.run(
            [sys.executable, str(VERIFY_CLAIMS), str(self.memo), *args],
            capture_output=True, text=True)

    def test_missing_required_claim_fails_naming_it(self):
        self.memo.write_text("# memo\n\nclaim: net_return_pct = 22.8\n")
        r = self.run_check("--require", "net_return_pct",
                           "--require", "max_drawdown_pct")
        self.assertNotEqual(r.returncode, 0,
                            "absence must FAIL the gate, not skip it (F6)")
        self.assertIn("max_drawdown_pct", r.stdout + r.stderr)

    def test_typoed_claim_line_counts_as_absent(self):
        self.memo.write_text("# memo\n\nclam: net_return_pct = 22.8\n")
        r = self.run_check("--require", "net_return_pct")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("net_return_pct", r.stdout + r.stderr)

    def test_refuted_claim_fails_with_both_values(self):
        self.memo.write_text("claim: net_return_pct = 400.0\n")
        r = self.run_check("--require", "net_return_pct",
                           "--actual", "net_return_pct=22.8")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("400.0", r.stdout + r.stderr)
        self.assertIn("22.8", r.stdout + r.stderr)

    def test_all_required_present_and_true_passes(self):
        self.memo.write_text("claim: net_return_pct = 22.8\n"
                             "claim: max_drawdown_pct = 49.5\n")
        r = self.run_check("--require", "net_return_pct",
                           "--require", "max_drawdown_pct",
                           "--actual", "net_return_pct=22.8",
                           "--actual", "max_drawdown_pct=49.5")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
