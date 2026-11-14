"""P7 tests: the measurement subsystem works in a STAMPED project layout.

The unit tests in core/tests/eval/ cover the statistics and the corpus loader; this covers the
seams that only exist after `gategraph init`: the harness/core/eval/cli.py sys.path
bootstrap, the default harness/eval/corpus location, and the failure ledger
landing in the project's harness/state/.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

TEMPLATE = Path(__file__).resolve().parent.parent
FH = TEMPLATE / "gategraph.py"
# test_stamp_v2 owns the venv and gate helpers; reuse them (see its docstring).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_stamp_v2 import gate, require_template_venv, seed_venv  # noqa: E402

ANSWERS = {
    "project": "evaltoy",
    "artifact_noun": "note",
    "unit_dir": "notes",
    "gates": ["composed", "checked", "verified"],
    "hashed_gates": [],
    "seed_deny": True,  # stamps the example acct deny policy
}

ACCT = "010190" + "-" + "1234"  # assembled, no literal on disk


class TestEvalInStampedProject(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / "evaltoy"
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(ANSWERS, f)
        proc = subprocess.run([sys.executable, str(FH), "init", str(cls.root),
                               "--answers", f.name],
                              capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"init failed:\n{proc.stdout}\n{proc.stderr}")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _cli(self, *args):
        return subprocess.run(
            [sys.executable, "harness/core/eval/cli.py", *args],
            cwd=self.root, capture_output=True, text=True)

    def test_make_eval_green_in_stamped_layout(self):
        cdir = self.root / "harness" / "eval" / "corpus"
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "cases.yaml").write_text(yaml.safe_dump([
            {"id": "acct-catch", "kind": "detection", "set": "heldout",
             "gate": "checked", "path": "notes/n1/sketch.md",
             "artifact": "id: {{ACCOUNT}}\n", "expect": {"fire": ["acct"], "quiet": []}},
            {"id": "clean", "kind": "detection", "set": "iterate",
             "gate": "checked", "path": "notes/n1/sketch.md",
             "artifact": "an ordinary note\n", "expect": {"fire": [], "quiet": ["*"]}},
            {"id": "mach-order", "kind": "machinery", "set": "heldout",
             "gate": "checked", "scenario": "out_of_order_refused"},
        ]))
        p = self._cli("run", "--root", ".", "--seeds", "3")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        for token in ("CI", "ICC", "histogram"):
            self.assertIn(token, p.stdout)

    def test_regression_exits_one_and_lands_in_project_ledger(self):
        cdir = self.root / "harness" / "eval" / "alt-corpus"
        cdir.mkdir(parents=True, exist_ok=True)
        (cdir / "cases.yaml").write_text(yaml.safe_dump([
            {"id": "miss", "kind": "detection", "set": "heldout",
             "gate": "checked", "path": "notes/n1/sketch.md",
             "artifact": "nothing to catch\n",
             "expect": {"fire": ["acct"], "quiet": []}},
        ]))
        p = self._cli("run", "--root", ".", "--corpus", str(cdir), "--seeds", "2")
        self.assertEqual(p.returncode, 1)
        ledger = self.root / "harness" / "state" / "failures.jsonl"
        self.assertTrue(ledger.exists())
        rows = [json.loads(x) for x in ledger.read_text().splitlines()]
        self.assertTrue(any(r["kind"] == "eval_case" and r["case"] == "miss"
                            for r in rows))

    def test_gate_refusal_lands_in_project_ledger(self):
        # The refusal is written by core/kernel/ledger.py through the gate CLI, which needs the
        # project venv (LangGraph). The ledger is harness/state/failures.jsonl (failures_path).
        require_template_venv()
        seed_venv(self.root)
        self.assertEqual(gate(self.root, "start", "--unit", "n1").returncode, 0)
        p = gate(self.root, "attempt", "checked", "--unit", "n1")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)  # out of order: composed not passed
        ledger = self.root / "harness" / "state" / "failures.jsonl"
        rows = [json.loads(x) for x in ledger.read_text().splitlines()]
        self.assertTrue(any(r["kind"] == "gate_refusal" and r.get("verb") == "attempt"
                            and r.get("unit") == "n1" and "composed" in r.get("reason", "")
                            for r in rows), rows)


if __name__ == "__main__":
    unittest.main()
