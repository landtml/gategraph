"""The tier instruments on the kernel ledger, ported from the legacy core/tests/test_instruments.py
(X1 B5b): T2 triage report (ledger.report and the report subcommand), T3 escalation tally and
labels (ledger.label and escalation_tally).

Run alone with: python3 core/tests/kernel/test_ledger_report.py
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (repository root on sys.path)

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from core.kernel import ledger
from fixtures_v2 import make_project

ROWS = [
    {"at": "2026-06-01T10:00:00", "kind": "gate_refusal", "gate": "lint_passed", "unit": "u1"},
    {"at": "2026-06-02T11:00:00", "kind": "gate_refusal", "gate": "lint_passed", "unit": "u1"},
    {"at": "2026-06-03T12:00:00", "kind": "gate_refusal", "gate": "composed", "unit": "u2"},
    {"at": "2026-07-01T09:00:00", "kind": "escalation", "unit": "u2", "note": "stuck"},
]


class TestLedgerReport(unittest.TestCase):
    """T2: recurring rows are the signal for new checks; the report makes recurrence visible."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_project(Path(self._tmp.name) / "p", "minimal")

    def test_groups_by_kind_with_gate_breakdown(self):
        out = ledger.report(ROWS)
        self.assertIn("gate_refusal", out)
        self.assertIn("3", out)            # 3 gate_refusal rows
        self.assertIn("lint_passed", out)  # per-gate breakdown
        self.assertIn("escalation", out)

    def test_month_trend(self):
        out = ledger.report(ROWS)
        self.assertIn("2026-06", out)
        self.assertIn("2026-07", out)

    def test_empty_ledger_says_so(self):
        self.assertIn("empty", ledger.report([]).lower())

    def test_cli_report_subcommand(self):
        for row in ROWS:
            ledger.record(self.root, row["kind"],
                          **{k: v for k, v in row.items() if k not in ("at", "kind")})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = ledger.main(["--root", str(self.root), "report"])
        self.assertEqual(rc, 0)
        self.assertIn("gate_refusal", buf.getvalue())


class TestEscalationTally(unittest.TestCase):
    """T3: the tally lets the user label each escalation worthy or not; precision appears once
    labels exist. The label is the user's; only the bookkeeping is code."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = make_project(Path(self._tmp.name) / "p", "minimal")
        # two escalations in the same second: the ambiguous-timestamp case by construction
        rows = [{"at": "2026-07-01T09:00:00", "kind": "escalation", "id": "e1aaaa01",
                 "unit": "u1", "note": "oracle kept raising"},
                {"at": "2026-07-01T09:00:00", "kind": "escalation", "id": "e2bbbb02",
                 "unit": "u2", "note": "fail streak"}]
        p = ledger.failures_path(self.root)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        self.esc1, self.esc2 = rows

    def tally(self):
        return ledger.escalation_tally(ledger.read_ledger(self.root))

    def test_lists_escalations_unlabeled(self):
        out = self.tally()
        self.assertIn("u1", out)
        self.assertIn("u2", out)
        self.assertIn("unlabeled", out)

    def test_label_then_precision(self):
        ledger.label(self.root, True, ref=self.esc1["id"], unit="u1")
        out = self.tally()
        self.assertIn("precision", out)
        self.assertIn("1/1", out)
        self.assertIn("20", out, "must remind that T3 needs at least 20 labeled real events")

    def test_relabel_latest_wins(self):
        ledger.label(self.root, True, ref=self.esc1["id"], unit="u1")
        ledger.label(self.root, False, ref=self.esc1["id"], unit="u1")
        self.assertIn("0/1", self.tally())

    def test_ambiguous_at_without_unit_refused(self):
        # both fixture rows may share one same-second timestamp: a bare --at must be refused
        # and must label nothing
        with self.assertRaises(LookupError) as ctx:
            ledger.label(self.root, True, at=self.esc1["at"])
        self.assertIn("ambiguous", str(ctx.exception).lower())
        self.assertIn("unlabeled", self.tally())

    def test_label_unknown_at_refused(self):
        with self.assertRaises(LookupError) as ctx:
            ledger.label(self.root, True, at="2020-01-01T00:00:00")
        self.assertIn("no escalation", str(ctx.exception).lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
