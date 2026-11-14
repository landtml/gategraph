"""Corpus loader and trial perturbation (core/eval/corpus.py).

Ported from the legacy core/tests/test_eval.py (X1 B1). Offline; no model.
Run standalone: ``.venv/bin/python core/tests/eval/test_corpus.py``.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401  (puts the repository root on sys.path)

import tempfile
import unittest
from pathlib import Path

import yaml

from core.eval import corpus
from core.eval.corpus import CorpusError

# Synthetic ACCT-shaped token, built by concatenation so no literal sits in source.
ACCT_TOKEN = "010190" + "-1234"

CORPUS_OK = [
    {"id": "det-acct", "kind": "detection", "set": "heldout", "gate": "verified",
     "adversarial": False, "path": "units/x/sketch.md",
     "artifact": f"contact line\nid: {ACCT_TOKEN}\n",
     "expect": {"fire": ["acct"], "quiet": []}},
    {"id": "det-clean", "kind": "detection", "set": "iterate", "gate": "verified",
     "adversarial": False, "path": "units/x/sketch.md",
     "artifact": "a perfectly ordinary paragraph about walking outside.\n",
     "expect": {"fire": [], "quiet": ["*"]}},
]


class TestCorpusLoader(unittest.TestCase):
    def _load(self, cases):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "cases.yaml").write_text(yaml.safe_dump(cases))
            return corpus.load_corpus(Path(td))

    def test_valid_corpus_loads(self):
        cases = self._load(CORPUS_OK)
        self.assertEqual([c["id"] for c in cases], ["det-acct", "det-clean"])  # sorted

    def test_duplicate_id_rejected(self):
        with self.assertRaisesRegex(CorpusError, "det-acct"):
            self._load(CORPUS_OK + [dict(CORPUS_OK[0])])

    def test_unknown_kind_rejected(self):
        bad = dict(CORPUS_OK[0], id="x", kind="vibes")
        with self.assertRaisesRegex(CorpusError, "vibes"):
            self._load([bad])

    def test_detection_without_artifact_rejected(self):
        bad = {k: v for k, v in CORPUS_OK[0].items() if k != "artifact"}
        with self.assertRaisesRegex(CorpusError, "artifact"):
            self._load([bad])

    def test_unknown_machinery_scenario_rejected(self):
        bad = {"id": "m1", "kind": "machinery", "set": "heldout",
               "gate": "checked", "scenario": "does-not-exist"}
        with self.assertRaisesRegex(CorpusError, "does-not-exist"):
            self._load([bad])

    def test_acct_placeholder_expanded_at_load(self):
        # Corpus files never store a ACCT-shaped literal on disk; the loader expands
        # {{ACCOUNT}} into the synthetic token.
        c = dict(CORPUS_OK[0], id="x", artifact="id: {{ACCOUNT}}\n")
        loaded = self._load([c])
        case = next(k for k in loaded if k["id"] == "x")
        self.assertIn(ACCT_TOKEN[:6], case["artifact"])
        self.assertNotIn("{{ACCOUNT}}", case["artifact"])

    def test_bad_split_rejected(self):
        bad = dict(CORPUS_OK[0], id="x", set="training")
        with self.assertRaisesRegex(CorpusError, "training"):
            self._load([bad])


class TestPerturb(unittest.TestCase):
    def test_seed_zero_is_identity(self):
        self.assertEqual(corpus.perturb("a\nb\n", 0), "a\nb\n")

    def test_deterministic_per_seed(self):
        self.assertEqual(corpus.perturb("a\nb\n", 3), corpus.perturb("a\nb\n", 3))
        self.assertNotEqual(corpus.perturb("a\nb\n", 3), corpus.perturb("a\nb\n", 4))

    def test_original_lines_survive_in_order(self):
        original = ["alpha one", "beta two", "gamma three"]
        mutated = corpus.perturb("\n".join(original) + "\n", 7).splitlines()
        kept = [ln for ln in mutated if ln in original]
        self.assertEqual(kept, original)  # insertions only, lines never edited


if __name__ == "__main__":
    unittest.main()
