"""Artifact readers by file type (B11): binary files are never scanned as text,
PDFs are scanned as extracted text, notebooks as their cell sources."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401

import hashlib
import json
import sys as _sys
import tempfile
import unittest
from pathlib import Path

from core.kernel import extract, policy
from core.kernel.types import Severity

# Assembled at run time: never a literal, account-number-shaped string on disk.
ACCT = "010190" + "-" + "1234"


def _acct_policies():
    return policy.compile_policies(_FakeCfg([
        {"id": "acct", "regex": r"\d{6}\s*-\s*\d{4}", "severity": "fatal",
         "summary": "no account number"}]))


class _FakeCfg:
    def __init__(self, deny):
        self.deny = deny


class TestArtifactReaders(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        (self.root / "dossiers" / "case").mkdir(parents=True)

    def tearDown(self):
        self._td.cleanup()

    def test_B11_binary_file_not_scanned_as_text(self):
        blob = b"\x00\x01\x02" + ACCT.encode("ascii") + b"\x00\xff"
        path = self.root / "dossiers" / "case" / "blob.tex"
        path.write_bytes(blob)
        # The same bytes read as text would fire, so the test has teeth.
        self.assertTrue(policy.scan_text(blob.decode("utf-8", "replace"),
                                         _acct_policies(), "dossiers/case/blob.tex"))
        result = policy.scan_path(path, self.root, _acct_policies())
        self.assertEqual(result.findings, [])
        self.assertEqual([n["note"] for n in result.notes], ["skipped_binary"])
        self.assertEqual(result.notes[0]["sha256"], hashlib.sha256(blob).hexdigest())

    def test_B11_pdf_scanned_as_extracted_text(self):
        path = self.root / "dossiers" / "case" / "summary.pdf"
        # Raw PDF bytes carry the token in metadata; the reader must not scan them.
        path.write_bytes(b"%PDF-1.4\n1 0 obj << /Title (" + ACCT.encode() + b") >> endobj\n")

        missing = ("no-such-pdf-extractor-xyz", "{path}")
        result = policy.scan_path(path, self.root, _acct_policies(), pdf_command=missing)
        self.assertEqual(result.findings, [])
        self.assertEqual([n["note"] for n in result.notes], ["skipped_pdf"])
        self.assertIn("not installed", result.notes[0]["reason"])

        # An extractor that emits the visible text. The finding must come from it.
        emit = (_sys.executable, "-c",
                "import sys; print('Contact ' + '" + ACCT + "')", "{path}")
        result = policy.scan_path(path, self.root, _acct_policies(), pdf_command=emit)
        self.assertEqual(result.notes, [])
        self.assertEqual([(f.rule, f.line, f.severity) for f in result.findings],
                         [("acct", 1, Severity.FATAL)])

    def test_notebook_scans_cell_sources_only(self):
        nb = {"cells": [
            {"cell_type": "markdown", "source": ["# Title\n", "plain"]},
            {"cell_type": "code", "source": "x = 1",
             "outputs": [{"text": ["Contact " + ACCT]}]},
        ], "nbformat": 4}
        path = self.root / "dossiers" / "case" / "notes.ipynb"
        path.write_text(json.dumps(nb), encoding="utf-8")
        art = extract.read_artifact(path)
        self.assertEqual(art.kind, "notebook")
        self.assertIn("# Title\nplain", art.text)
        self.assertIn("x = 1", art.text)
        self.assertNotIn(ACCT, art.text)

    def test_text_file_is_read_with_universal_newlines(self):
        path = self.root / "dossiers" / "case" / "main.tex"
        path.write_bytes(b"one\r\ntwo\rthree\n")
        art = extract.read_artifact(path)
        self.assertEqual(art.kind, "text")
        self.assertEqual(art.text, "one\ntwo\nthree\n")
        self.assertIsNone(art.note)


if __name__ == "__main__":
    unittest.main()
