"""Tests for core/kernel/ledger.py: same-second rows are distinguishable (B53),
escalation labels resolve to one row, and rows written by the old module load.

Standard library only. Runs with the system python3 and with .venv/bin/python.
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from core.kernel import ledger

REPO = _bootstrap.REPO
MODULE_PATH = REPO / "core" / "kernel" / "ledger.py"
SAME_SECOND = "2026-01-02T03:04:05"


def make_root(base):
    adapter = pathlib.Path(base) / "harness" / "adapter"
    adapter.mkdir(parents=True, exist_ok=True)
    (adapter / "adapter.yaml").write_text("project: example\n", encoding="utf-8")
    return pathlib.Path(base).resolve()


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = make_root(pathlib.Path(tmp.name))
        clock = mock.patch.object(ledger, "_now_ts", return_value=SAME_SECOND)
        clock.start()
        self.addCleanup(clock.stop)


class TestRecord(Base):
    def test_B53_same_second_rows_distinguishable(self):
        a = ledger.record(self.root, "escalation", unit="u1", note="first")
        b = ledger.record(self.root, "escalation", unit="u1", note="second")
        self.assertEqual(a["at"], b["at"])
        self.assertNotEqual(a["id"], b["id"])
        rows = ledger.read_ledger(self.root)
        self.assertEqual(len({r["id"] for r in rows}), 2)

    def test_B53_label_by_id_marks_only_its_own_row(self):
        a = ledger.record(self.root, "escalation", unit="u1", note="first")
        b = ledger.record(self.root, "escalation", unit="u1", note="second")
        ledger.label(self.root, ref=b["id"], worthy=True, unit="u1")
        lines = ledger.escalation_tally(ledger.read_ledger(self.root)).splitlines()
        first = next(l for l in lines if "first" in l)
        second = next(l for l in lines if "second" in l)
        self.assertIn("unlabeled", first)
        self.assertIn("worthy", second)
        self.assertNotIn("not-worthy", second)

    def test_record_never_raises_on_unwritable_ledger(self):
        blocker = self.root / "harness" / "state"
        blocker.parent.mkdir(parents=True, exist_ok=True)
        blocker.write_text("a file, not a directory\n", encoding="utf-8")
        row = ledger.record(self.root, "correction", note="still returns")
        self.assertEqual(row["kind"], "correction")

    def test_read_ledger_last(self):
        for i in range(3):
            ledger.record(self.root, "refusal", note=str(i))
        self.assertEqual([r["note"] for r in ledger.read_ledger(self.root, last=2)],
                         ["1", "2"])


class TestLegacyRows(Base):
    def test_rows_written_by_the_old_module_load_and_label(self):
        p = self.root / "harness" / "state" / "failures.jsonl"
        p.parent.mkdir(parents=True)
        p.write_text(
            json.dumps({"at": "2026-01-01T10:00:00", "kind": "escalation",
                        "unit": "u9", "note": "old"}) + "\n"
            "torn row{\n", encoding="utf-8")
        rows = ledger.read_ledger(self.root)
        self.assertEqual(len(rows), 1)
        ledger.label(self.root, ref="2026-01-01T10:00:00", worthy=False, unit="u9")
        self.assertIn("not-worthy", ledger.escalation_tally(ledger.read_ledger(self.root)))

    def test_label_refuses_unknown_and_ambiguous_refs(self):
        ledger.record(self.root, "escalation", unit="u1", note="a")
        with self.assertRaises(LookupError):
            ledger.label(self.root, ref="nope", worthy=True)
        r1 = ledger.record(self.root, "escalation", unit="u2", note="b")
        ledger.record(self.root, "escalation", unit="u2", note="c")
        with self.assertRaises(LookupError):
            ledger.label(self.root, ref=SAME_SECOND, worthy=True, unit="u2")
        ledger.label(self.root, ref=r1["id"], worthy=True, unit="u2")


class TestCli(Base):
    def test_root_before_subcommand(self):
        proc = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--root", str(self.root),
             "record", "--kind", "correction", "--note", "x"],
            capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(len(ledger.read_ledger(self.root)), 1)


if __name__ == "__main__":
    unittest.main()
