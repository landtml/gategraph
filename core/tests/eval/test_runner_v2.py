"""V1: the new deterministic runner (core/eval/runner_v2.py) over eval/corpus with eval/pack_v2.

Checks, in order:
1. per-case pass/fail equals the stored golden of the legacy runner (core/tests/eval/
   legacy_per_case.json, captured from core/eval/runner.py with eval/pack before its deletion in
   X1 B1) for all 44 non-live cases over 5 seeds;
2. crippling one policy pattern in a temporary copy of the pack fails exactly that policy's cases;
3. the overall bucket is 220/220 with the pack as shipped.

Run standalone: ``.venv/bin/python core/tests/eval/test_runner_v2.py``. Offline only: no model.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import contextlib
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from core.eval import corpus
from core.eval import runner_v2
from core.kernel import ledger

ROOT = Path(_bootstrap.REPO)
CORPUS = ROOT / "eval" / "corpus"
PACK_V2 = ROOT / "eval" / "pack_v2"
LEGACY_GOLDEN = Path(__file__).resolve().parent / "legacy_per_case.json"
SEEDS = 5
CRIPPLED = r"\d{6}\s*-\s*\d{4}"


def _cases():
    return corpus.load_corpus(CORPUS)


class RunnerV2Parity(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.cases = _cases()
        cls.new = runner_v2.run_corpus(PACK_V2, CORPUS, seeds=SEEDS)
        # The legacy engine is gone; its per-case results were captured before deletion.
        cls.legacy = json.loads(LEGACY_GOLDEN.read_text(encoding="utf-8"))

    def test_same_cases_as_legacy(self):
        non_live = sorted(c["id"] for c in self.cases if c["kind"] != "live")
        self.assertEqual(len(non_live), 44)
        self.assertEqual(sorted(self.new["cases"]), non_live)
        self.assertEqual(sorted(self.legacy["cases"]), non_live)

    def test_per_case_passes_match_legacy(self):
        for cid, legacy_rec in self.legacy["cases"].items():
            new_rec = self.new["cases"][cid]
            self.assertEqual(new_rec["passes"], legacy_rec["passes"],
                             f"{cid}: new {new_rec['passes']} != legacy {legacy_rec['passes']}")

    def test_overall_is_220_of_220(self):
        self.assertEqual(self.new["overall"]["trials"], 220)
        self.assertEqual(self.new["overall"]["passed"], 220)
        self.assertEqual(self.new["histogram"], {})

    def test_live_cases_are_skipped(self):
        live = sum(1 for c in self.cases if c["kind"] == "live")
        self.assertEqual(self.new["live_cases_skipped"], live)


class CrippledPolicy(unittest.TestCase):

    def test_crippling_acct_fails_exactly_its_cases(self):
        cases = _cases()
        with tempfile.TemporaryDirectory() as td:
            pack = Path(td) / "pack_v2"
            shutil.copytree(PACK_V2, pack)
            adapter = pack / "harness" / "adapter" / "adapter.yaml"
            text = adapter.read_text(encoding="utf-8")
            self.assertEqual(text.count(CRIPPLED), 1, "the acct pattern must appear once")
            adapter.write_text(text.replace(CRIPPLED, "(?!)"), encoding="utf-8")
            rep = runner_v2.run_corpus(pack, CORPUS, seeds=SEEDS)
        expected = {c["id"] for c in cases
                    if c["kind"] == "detection" and "acct" in c["expect"]["fire"]}
        self.assertTrue(expected, "the corpus must have acct-firing cases")
        failed = {cid for cid, rec in rep["cases"].items() if not all(rec["passes"])}
        self.assertEqual(failed, expected)
        gates = {c["gate"] for c in cases if c["id"] in expected}
        self.assertEqual(set(rep["histogram"]), gates)


# Blocks the LangGraph stack the way a system python3 without the venv sees it.
BLOCK_LANGGRAPH = ("import sys\n"
                   "sys.modules['langgraph'] = None\n"
                   "sys.modules['langgraph.types'] = None\n")


def _kinds(cases):
    return {c["kind"] for c in cases if c["kind"] != "live"}


class DetectionWithoutLangGraph(unittest.TestCase):
    """Stamped projects run `make eval` with system python3, which has no langgraph."""

    def test_detection_runs_without_langgraph(self):
        script = BLOCK_LANGGRAPH + (
            "import json, sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from core.eval import runner_v2\n"
            f"rep = runner_v2.run_corpus({str(PACK_V2)!r}, {str(CORPUS)!r}, seeds=2)\n"
            "print(json.dumps({'overall': rep['overall'], 'kinds': sorted({r['kind'] "
            "for r in rep['cases'].values()}), 'skipped': rep['machinery_skipped']}))\n")
        proc = subprocess.run([sys.executable, "-c", script], capture_output=True,
                              text=True, cwd=str(ROOT))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        n_det = sum(1 for c in _cases() if c["kind"] == "detection")
        self.assertTrue(n_det > 0)
        self.assertEqual(out["kinds"], ["detection"])
        self.assertEqual(out["overall"]["trials"], 2 * n_det)
        self.assertEqual(out["overall"]["passed"], 2 * n_det)
        self.assertGreater(out["skipped"], 0)


class MachinerySkippedWithoutVenv(unittest.TestCase):

    def test_machinery_skipped_not_passed_without_venv(self):
        n_mach = sum(1 for c in _cases() if c["kind"] == "machinery")
        n_det = sum(1 for c in _cases() if c["kind"] == "detection")
        self.assertTrue(n_mach > 0)
        with mock.patch.object(runner_v2, "_load_langgraph", return_value=False):
            rep = runner_v2.run_corpus(PACK_V2, CORPUS, seeds=2)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = runner_v2.main(["--pack", str(PACK_V2), "--corpus", str(CORPUS),
                                       "--seeds", "2"])
        self.assertEqual(rep["machinery_skipped"], n_mach)
        self.assertTrue(all(r["kind"] != "machinery" for r in rep["cases"].values()))
        self.assertEqual(rep["overall"]["trials"], 2 * n_det)
        self.assertEqual(rep["overall"]["passed"], 2 * n_det)
        self.assertEqual(rep["histogram"], {})
        self.assertEqual(code, 0)
        text = buf.getvalue()
        self.assertIn(f"{n_mach} cases skipped (no venv)", text)
        self.assertIn("skipped (no venv)", text)
        self.assertIn("neither passed nor failed", text)


class FailureLedgerWritten(unittest.TestCase):

    def test_failure_ledger_written(self):
        with tempfile.TemporaryDirectory() as td:
            cdir = Path(td) / "corpus"
            cdir.mkdir()
            (cdir / "cases.yaml").write_text(yaml.safe_dump([
                {"id": "miss", "kind": "detection", "set": "heldout", "gate": "checked",
                 "path": "notes/n1/sketch.md", "artifact": "nothing to catch\n",
                 "expect": {"fire": ["acct"], "quiet": []}},
                {"id": "clean", "kind": "detection", "set": "iterate", "gate": "checked",
                 "path": "notes/n1/sketch.md", "artifact": "an ordinary note\n",
                 "expect": {"fire": [], "quiet": ["*"]}},
            ]))
            root = Path(td) / "project"
            with mock.patch.object(runner_v2, "_load_langgraph", return_value=False):
                rep = runner_v2.run_corpus(PACK_V2, cdir, seeds=2, root=root)
            rows = ledger.read_ledger(root)
            ledger_file = root / "harness" / "state" / "failures.jsonl"
            self.assertTrue(ledger_file.is_file())
        self.assertEqual(rep["histogram"], {"checked": 2})
        self.assertEqual(len(rows), 1, rows)
        row = rows[0]
        self.assertEqual(row["kind"], "eval_case")
        self.assertEqual(row["case"], "miss")
        self.assertEqual(row["gate"], "checked")
        self.assertEqual(row["case_kind"], "detection")
        self.assertEqual(row["failed_trials"], 2)
        self.assertEqual(row["trials"], 2)


if __name__ == "__main__":
    unittest.main()
