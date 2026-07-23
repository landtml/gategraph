"""The tier instruments (T2/T3/T13, code halves):
ledger triage report, escalation tally + labels, maintenance rituals.

The instruments are cheap by design, the EVIDENCE they exist to collect
is calendar-bound and user-owned; these tests pin only the tooling."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fixtures import mkroot  # noqa: E402


class TestLedgerReport(unittest.TestCase):
    """T2: recurring failures.jsonl rows are the signal for new gates.
    the report groups them so recurrence is visible without jq folklore."""

    ROWS = [
        {"at": "2026-06-01T10:00:00", "kind": "gate_refusal",
         "gate": "lint_passed", "unit": "u1"},
        {"at": "2026-06-02T11:00:00", "kind": "gate_refusal",
         "gate": "lint_passed", "unit": "u1"},
        {"at": "2026-06-03T12:00:00", "kind": "gate_refusal",
         "gate": "composed", "unit": "u2"},
        {"at": "2026-07-01T09:00:00", "kind": "escalation", "unit": "u2",
         "note": "stuck"},
    ]

    def test_groups_by_kind_with_gate_breakdown(self):
        from core import failures
        out = failures.report(self.ROWS)
        self.assertIn("gate_refusal", out)
        self.assertIn("3", out)          # 3 gate_refusal rows
        self.assertIn("lint_passed", out)  # per-gate breakdown
        self.assertIn("escalation", out)

    def test_month_trend(self):
        from core import failures
        out = failures.report(self.ROWS)
        self.assertIn("2026-06", out)
        self.assertIn("2026-07", out)

    def test_empty_ledger_says_so(self):
        from core import failures
        self.assertIn("empty", failures.report([]).lower())

    def test_cli_report_subcommand(self):
        from core import failures
        with tempfile.TemporaryDirectory() as td:
            root = mkroot(Path(td))
            for row in self.ROWS:
                failures.record(root, row["kind"],
                                **{k: v for k, v in row.items()
                                   if k not in ("at", "kind")})
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = failures.main(["--root", str(root), "report"])
            self.assertEqual(rc, 0)
            self.assertIn("gate_refusal", buf.getvalue())


class TestEscalationTally(unittest.TestCase):
    """T3: escalation exists so the harness knows when it doesn't know.
    the tally lets the user label each event human-worthy or not, and
    prints precision once labels exist. The label is inherently the
    user's; only the bookkeeping is code."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = mkroot(Path(self._tmp.name))
        from core import failures
        self.failures = failures
        self.esc1 = failures.record(self.root, "escalation", unit="u1",
                                    note="oracle kept raising")
        self.esc2 = failures.record(self.root, "escalation", unit="u2",
                                    note="fail streak")

    def tearDown(self):
        self._tmp.cleanup()

    def _cli(self, *args):
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = self.failures.main(["--root", str(self.root), *args])
        return rc, buf.getvalue(), err.getvalue()

    def test_lists_escalations_unlabeled(self):
        rc, out, _ = self._cli("escalations")
        self.assertEqual(rc, 0)
        self.assertIn("u1", out)
        self.assertIn("u2", out)
        self.assertIn("unlabeled", out)

    def test_label_then_precision(self):
        rc, out, _ = self._cli("label", "--at", self.esc1["at"],
                               "--unit", "u1", "--worthy", "yes")
        self.assertEqual(rc, 0, out)
        rc, out, _ = self._cli("escalations")
        self.assertEqual(rc, 0)
        self.assertIn("precision", out)
        self.assertIn("1/1", out)
        self.assertIn("20", out,
                      "must remind that T3 needs ≥20 labeled real events")

    def test_relabel_latest_wins(self):
        self._cli("label", "--at", self.esc1["at"], "--unit", "u1",
                  "--worthy", "yes")
        self._cli("label", "--at", self.esc1["at"], "--unit", "u1",
                  "--worthy", "no")
        rc, out, _ = self._cli("escalations")
        self.assertIn("0/1", out)

    def test_ambiguous_at_without_unit_refused(self):
        # both fixture events share one same-second timestamp, a bare
        # --at must be refused, not silently label more than one event
        rc, out, err = self._cli("label", "--at", self.esc1["at"],
                                 "--worthy", "yes")
        self.assertEqual(rc, 1)
        self.assertIn("ambiguous", (out + err).lower())
        rc, out, _ = self._cli("escalations")
        self.assertIn("unlabeled", out, "nothing may have been labeled")

    def test_label_unknown_at_refused(self):
        rc, out, err = self._cli("label", "--at", "2020-01-01T00:00:00",
                                 "--worthy", "yes")
        self.assertEqual(rc, 1)
        self.assertIn("no escalation", (out + err).lower())


class TestMaintenance(unittest.TestCase):
    """T13: the make-target checklist that prints what's due, and the
    maintenance log (harness/state/maintenance.jsonl) that later proves
    the rhythm happened."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = mkroot(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def _cli(self, *args):
        from core import maintenance
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = maintenance.main(["--root", str(self.root), *args])
        return rc, buf.getvalue(), err.getvalue()

    def test_fresh_root_everything_due(self):
        rc, out, _ = self._cli("status")
        self.assertEqual(rc, 0, "the checklist informs; it never gates")
        self.assertIn("corpus-rotation", out)
        self.assertIn("ledger-triage", out)
        self.assertIn("never", out)
        self.assertIn("DUE", out)

    def test_done_records_and_clears_due(self):
        rc, out, _ = self._cli("done", "ledger-triage", "--note", "clean")
        self.assertEqual(rc, 0, out)
        rc, out, _ = self._cli("status")
        triage_line = next(ln for ln in out.splitlines()
                           if "ledger-triage" in ln)
        self.assertNotIn("DUE", triage_line)
        log = (self.root / "harness" / "state" / "maintenance.jsonl")
        self.assertTrue(log.is_file(), "the rhythm must leave a receipt")

    def test_unknown_ritual_refused(self):
        rc, out, err = self._cli("done", "bogus-ritual")
        self.assertEqual(rc, 1)
        self.assertIn("unknown ritual", (out + err).lower())

    def test_flags_verify_only_when_loop_enabled(self):
        rc, out, _ = self._cli("status")
        self.assertNotIn("flags-verify", out,
                         "loop-disabled projects have no FLAGS pin to verify")
        import yaml
        apath = self.root / "harness" / "adapter" / "adapter.yaml"
        adapter = yaml.safe_load(apath.read_text())
        adapter["loop"]["enabled"] = True
        apath.write_text(yaml.safe_dump(adapter))
        rc, out, _ = self._cli("status")
        self.assertIn("flags-verify", out)


# In the template repo parents[2] is the repo root and seed/ exists; in a
# STAMPED project this file lives under harness/core/tests and there is no
# seed, the seed-surface assertion is template-only and skips there (the
# same convention as test_schema_surface.py).
TEMPLATE = Path(__file__).resolve().parents[2]
template_only = unittest.skipUnless((TEMPLATE / "seed").is_dir(),
                                    "template repo only (no seed/ in stamps)")


class TestMaintenanceMakeTargets(unittest.TestCase):
    """The ritual is only low-friction if `make maintenance` exists, in
    the template AND in every stamped project (seed Makefile)."""

    @template_only
    def test_template_makefile_target(self):
        # Template-only on purpose: the Makefile is a SEED file (stamped
        # once, never updated), so a pre-0.4 project that fh-updates its
        # core must not fail this, new core tests may not assert on
        # seed-generation surface.
        text = (TEMPLATE / "Makefile").read_text(encoding="utf-8")
        self.assertIn("maintenance:", text)
        self.assertIn("core/maintenance.py", text)

    @template_only
    def test_seed_makefile_target(self):
        text = (TEMPLATE / "seed" / "Makefile").read_text(encoding="utf-8")
        self.assertIn("maintenance:", text)
        self.assertIn("harness/core/maintenance.py", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
