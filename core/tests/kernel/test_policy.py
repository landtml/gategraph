"""Policy scanner: the detection corpus must reproduce the legacy golden
snapshot (tests/golden/expected/_detection.json) for every case and seed."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import _bootstrap  # noqa: F401

import json
import unittest
from pathlib import Path

from core.kernel import config
from core.eval import corpus
from core.kernel import policy
from core.kernel.types import Finding, Severity

REPO = _bootstrap.REPO
PACK = REPO / "core" / "tests" / "kernel" / "fixtures" / "eval_pack_v1"  # schema-1 pack copy (D6)
CORPUS = REPO / "eval" / "corpus"
GOLDEN = REPO / "tests" / "golden" / "expected" / "_detection.json"
SEEDS = 5


def _compiled():
    cfg = config.load(PACK)
    return policy.compile_policies(cfg)


def _trial_pass(case, fired):
    # Same rule as the legacy runner _detection_trial (legacy-0.4), applied to our fired set.
    exp = case["expect"]
    if any(p not in fired for p in exp["fire"]):
        return False
    if "*" in exp["quiet"]:
        return not fired
    return not any(p in fired for p in exp["quiet"])


def _as_tuple(f: Finding):
    return [f.rule, f.severity.value, f.line, f.message]


class TestDetectionGolden(unittest.TestCase):
    def test_every_detection_case_matches_golden_fire_and_quiet_sets(self):
        golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
        pols = _compiled()
        cases = [c for c in corpus.load_corpus(CORPUS) if c["kind"] == "detection"]
        self.assertEqual(sorted(c["id"] for c in cases), sorted(golden))
        mismatches = []
        for case in cases:
            gold = golden[case["id"]]
            self.assertEqual(case["path"], gold["path"])
            base = policy.scan_text(corpus.perturb(case["artifact"], 0), pols, case["path"])
            if [_as_tuple(f) for f in base] != gold["findings"]:
                mismatches.append((case["id"], "findings"))
            for seed_rec in gold["seeds"]:
                text = corpus.perturb(case["artifact"], seed_rec["seed"])
                found = policy.scan_text(text, pols, case["path"])
                fired = sorted({f.rule for f in found})
                if fired != seed_rec["fired"]:
                    mismatches.append((case["id"], seed_rec["seed"], "fired"))
                if _trial_pass(case, set(fired)) != seed_rec["trial_pass"]:
                    mismatches.append((case["id"], seed_rec["seed"], "trial"))
        self.assertEqual(mismatches, [])


class TestScanner(unittest.TestCase):
    def setUp(self):
        self.pols = policy.compile_policies(_FakeCfg([
            {"id": "word", "regex": r"forbidden", "severity": "fatal",
             "summary": "no forbidden word"},
            {"id": "soft", "regex": r"advisory", "severity": "warn",
             "summary": "soft note"},
            {"id": "span", "regex": r"alpha\s+beta", "summary": "span"},
            {"id": "scoped", "regex": r"zeta", "scope": ["units/*/*.tex"],
             "summary": "scoped"},
        ]))

    def test_findings_are_typed_and_sorted_by_line_then_rule(self):
        text = "advisory\nforbidden here\n"
        found = policy.scan_text(text, self.pols, "units/a/x.tex")
        self.assertEqual([_as_tuple(f) for f in found],
                         [["soft", "warn", 1, "soft note"],
                          ["word", "fatal", 2, "no forbidden word"]])
        self.assertIsInstance(found[0], Finding)
        self.assertIs(found[0].severity, Severity.WARN)

    def test_match_across_a_line_break_is_attributed_to_first_line(self):
        found = policy.scan_text("x\nalpha\nbeta\n", self.pols, "units/a/x.tex")
        self.assertIn(["span", "fatal", 2, "span"], [_as_tuple(f) for f in found])

    def test_scope_uses_the_glob_dialect(self):
        self.assertEqual(policy.scan_text("zeta", self.pols, "units/a/x.tex")[0].rule,
                         "scoped")
        self.assertEqual(policy.scan_text("zeta", self.pols, "units/a/b/x.tex"), [])

    def test_normalize_folds_fullwidth_digits_and_strips_zero_width(self):
        self.assertEqual(policy.normalize("１２​3"), "123")

    def test_unknown_severity_is_refused_at_compile(self):
        with self.assertRaises(ValueError):
            policy.compile_policies(_FakeCfg([
                {"id": "bad", "regex": "x", "severity": "error", "summary": "s"}]))


class _FakeCfg:
    def __init__(self, deny):
        self.deny = deny


if __name__ == "__main__":
    unittest.main()
