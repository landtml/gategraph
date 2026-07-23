"""Unit tests for the measurement subsystem (§13): stats, ledger, corpus, runner, A/B."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # core/tests
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # `import core`

from fixtures import ACCT_TOKEN, mkroot, run_cli  # noqa: E402


class TestWilsonCI(unittest.TestCase):
    def test_midpoint_symmetric(self):
        from core.eval import stats
        lo, hi = stats.wilson_ci(5, 10)
        self.assertAlmostEqual(lo, 0.2366, places=3)
        self.assertAlmostEqual(hi, 0.7634, places=3)

    def test_zero_successes_lower_bound_is_zero(self):
        from core.eval import stats
        lo, hi = stats.wilson_ci(0, 10)
        self.assertEqual(lo, 0.0)
        self.assertGreater(hi, 0.0)  # Wilson never claims certainty from n=10

    def test_all_successes_upper_bound_is_one(self):
        from core.eval import stats
        lo, hi = stats.wilson_ci(10, 10)
        self.assertEqual(hi, 1.0)
        self.assertLess(lo, 1.0)

    def test_empty_sample_is_maximally_uncertain(self):
        from core.eval import stats
        self.assertEqual(stats.wilson_ci(0, 0), (0.0, 1.0))


class TestMcNemarExact(unittest.TestCase):
    # b = pairs where OFF passed / ON failed; c = OFF failed / ON passed.
    def test_six_discordant_one_direction(self):
        from core.eval import stats
        self.assertAlmostEqual(stats.mcnemar_exact(0, 6), 0.03125, places=6)

    def test_one_vs_eight(self):
        from core.eval import stats
        self.assertAlmostEqual(stats.mcnemar_exact(1, 8), 0.0390625, places=7)

    def test_balanced_discordance_clamps_to_one(self):
        from core.eval import stats
        self.assertEqual(stats.mcnemar_exact(3, 3), 1.0)

    def test_no_discordant_pairs_is_uninformative(self):
        from core.eval import stats
        self.assertEqual(stats.mcnemar_exact(0, 0), 1.0)

    def test_symmetric_in_arguments(self):
        from core.eval import stats
        self.assertEqual(stats.mcnemar_exact(2, 9), stats.mcnemar_exact(9, 2))


class TestICC1(unittest.TestCase):
    def test_perfect_within_case_consistency(self):
        from core.eval import stats
        # every case fully agrees with itself across seeds → ICC(1) = 1
        self.assertAlmostEqual(
            stats.icc1([[1, 1, 1], [0, 0, 0], [1, 1, 1]]), 1.0, places=9)

    def test_pure_noise_is_negative(self):
        from core.eval import stats
        # identical case means, all variance within-case → ICC(1) = -1 at n=2
        self.assertAlmostEqual(stats.icc1([[1, 0], [0, 1]]), -1.0, places=9)

    def test_degenerate_constant_data_is_one(self):
        from core.eval import stats
        # zero variance anywhere = trivially consistent, not a crash
        self.assertEqual(stats.icc1([[1, 1], [1, 1]]), 1.0)

    def test_single_case_is_undefined(self):
        from core.eval import stats
        self.assertIsNone(stats.icc1([[1, 0, 1]]))


class TestFailureLedger(unittest.TestCase):
    """§13: every gate failure and escalation lands in state/failures.jsonl.
    the mechanism by which recurring failures become new checks."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = mkroot(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def _rows(self):
        from core import failures
        return failures.read(self.root)

    def test_record_and_read_roundtrip(self):
        from core import failures
        failures.record(self.root, "correction", unit="u1", note="shipped wrong")
        failures.record(self.root, "gate_refusal", unit="u2", gate="verified")
        rows = failures.read(self.root)
        self.assertEqual([r["kind"] for r in rows], ["correction", "gate_refusal"])
        self.assertEqual(rows[0]["note"], "shipped wrong")
        self.assertTrue(all("at" in r for r in rows))

    def test_tool_set_refusal_lands_in_ledger(self):
        run_cli("gate.py", "start", "--unit", "u1", root=self.root)
        p = run_cli("gate.py", "approve", "verified", "--unit", "u1", root=self.root)
        self.assertEqual(p.returncode, 1)
        rows = self._rows()
        self.assertEqual(rows[-1]["kind"], "gate_refusal")
        self.assertEqual(rows[-1]["gate"], "verified")
        self.assertEqual(rows[-1]["unit"], "u1")

    def test_out_of_order_approve_lands_in_ledger(self):
        run_cli("gate.py", "start", "--unit", "u1", root=self.root)
        p = run_cli("gate.py", "approve", "checked", "--unit", "u1", root=self.root)
        self.assertEqual(p.returncode, 1)
        self.assertEqual(self._rows()[-1]["kind"], "gate_refusal")
        self.assertEqual(self._rows()[-1]["gate"], "checked")

    def test_escalation_lands_in_ledger(self):
        run_cli("gate.py", "start", "--unit", "u1", root=self.root)
        p = run_cli("gate.py", "escalate", "--unit", "u1", "--note", "stuck", root=self.root)
        self.assertEqual(p.returncode, 0)
        rows = self._rows()
        self.assertEqual(rows[-1]["kind"], "escalation")
        self.assertEqual(rows[-1]["note"], "stuck")

    def test_lint_fatal_lands_in_ledger(self):
        run_cli("gate.py", "start", "--unit", "u1", root=self.root)
        (self.root / "units" / "u1" / "sketch.md").write_text(f"id: {ACCT_TOKEN}\n")
        p = run_cli("lint.py", "--unit", "u1", "--set-gate", "verified", root=self.root)
        self.assertEqual(p.returncode, 2)
        rows = self._rows()
        self.assertEqual(rows[-1]["kind"], "lint_fatal")
        self.assertEqual(rows[-1]["gate"], "verified")
        self.assertIn("acct", " ".join(rows[-1]["findings"]))

    def test_clean_run_writes_nothing(self):
        run_cli("gate.py", "start", "--unit", "u1", root=self.root)
        run_cli("gate.py", "approve", "composed", "--unit", "u1", root=self.root)
        self.assertEqual(self._rows(), [])


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
        import yaml
        from core.eval import corpus
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "cases.yaml").write_text(yaml.safe_dump(cases))
            return corpus.load_corpus(Path(td))

    def test_valid_corpus_loads(self):
        cases = self._load(CORPUS_OK)
        self.assertEqual([c["id"] for c in cases], ["det-acct", "det-clean"])  # sorted

    def test_duplicate_id_rejected(self):
        from core.eval import corpus
        with self.assertRaisesRegex(corpus.CorpusError, "det-acct"):
            self._load(CORPUS_OK + [dict(CORPUS_OK[0])])

    def test_unknown_kind_rejected(self):
        from core.eval import corpus
        bad = dict(CORPUS_OK[0], id="x", kind="vibes")
        with self.assertRaisesRegex(corpus.CorpusError, "vibes"):
            self._load([bad])

    def test_detection_without_artifact_rejected(self):
        from core.eval import corpus
        bad = {k: v for k, v in CORPUS_OK[0].items() if k != "artifact"}
        with self.assertRaisesRegex(corpus.CorpusError, "artifact"):
            self._load([bad])

    def test_unknown_machinery_scenario_rejected(self):
        from core.eval import corpus
        bad = {"id": "m1", "kind": "machinery", "set": "heldout",
               "gate": "checked", "scenario": "does-not-exist"}
        with self.assertRaisesRegex(corpus.CorpusError, "does-not-exist"):
            self._load([bad])

    def test_cpr_placeholder_expanded_at_load(self):
        # Corpus files never store a ACCT-shaped literal on disk (same rule as
        # fixtures.py), the loader expands {{ACCOUNT}} into the synthetic token.
        c = dict(CORPUS_OK[0], id="x", artifact="id: {{ACCOUNT}}\n")
        loaded = self._load([c])
        case = next(k for k in loaded if k["id"] == "x")
        self.assertIn(ACCT_TOKEN[:6], case["artifact"])
        self.assertNotIn("{{ACCOUNT}}", case["artifact"])

    def test_bad_split_rejected(self):
        from core.eval import corpus
        bad = dict(CORPUS_OK[0], id="x", set="training")
        with self.assertRaisesRegex(corpus.CorpusError, "training"):
            self._load([bad])


class TestPerturb(unittest.TestCase):
    def test_seed_zero_is_identity(self):
        from core.eval import corpus
        self.assertEqual(corpus.perturb("a\nb\n", 0), "a\nb\n")

    def test_deterministic_per_seed(self):
        from core.eval import corpus
        self.assertEqual(corpus.perturb("a\nb\n", 3), corpus.perturb("a\nb\n", 3))
        self.assertNotEqual(corpus.perturb("a\nb\n", 3), corpus.perturb("a\nb\n", 4))

    def test_original_lines_survive_in_order(self):
        from core.eval import corpus
        original = ["alpha one", "beta two", "gamma three"]
        mutated = corpus.perturb("\n".join(original) + "\n", 7).splitlines()
        kept = [ln for ln in mutated if ln in original]
        self.assertEqual(kept, original)  # insertions only, lines never edited


class TestEvalRunner(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = mkroot(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, cases, seeds=3):
        from core import config
        from core.eval import runner
        return runner.run_corpus(self.root, config.load(self.root), cases, seeds=seeds)

    def test_detection_pass_and_report_shape(self):
        rep = self._run(CORPUS_OK, seeds=3)
        self.assertEqual(rep["trials_per_case"], 3)
        self.assertEqual(rep["cases"]["det-acct"]["passes"], [True, True, True])
        self.assertEqual(rep["cases"]["det-clean"]["passes"], [True, True, True])
        self.assertEqual(rep["overall"]["trials"], 6)
        self.assertEqual(rep["overall"]["pass_rate"], 1.0)
        self.assertIn("ci", rep["overall"])
        self.assertIn("heldout", rep["sets"])
        self.assertIn("iterate", rep["sets"])

    def test_missed_detection_fails_and_lands_everywhere(self):
        from core import failures
        miss = {"id": "det-miss", "kind": "detection", "set": "heldout",
                "gate": "verified", "path": "units/x/sketch.md",
                "artifact": "no token here\n", "expect": {"fire": ["acct"], "quiet": []}}
        rep = self._run(CORPUS_OK + [miss], seeds=2)
        self.assertEqual(rep["cases"]["det-miss"]["passes"], [False, False])
        self.assertEqual(rep["histogram"]["verified"], 2)  # failed trials per gate
        kinds = [r["kind"] for r in failures.read(self.root)]
        self.assertIn("eval_case", kinds)

    def test_false_positive_on_clean_case_fails(self):
        fp = {"id": "det-fp", "kind": "detection", "set": "iterate",
              "gate": "verified", "path": "units/x/sketch.md",
              "artifact": f"id: {ACCT_TOKEN}\n", "expect": {"fire": [], "quiet": ["*"]}}
        rep = self._run([fp], seeds=2)
        self.assertEqual(rep["cases"]["det-fp"]["passes"], [False, False])

    def test_machinery_scenarios_pass(self):
        mach = [
            {"id": "m-order", "kind": "machinery", "set": "heldout",
             "gate": "checked", "scenario": "out_of_order_refused"},
            {"id": "m-stale", "kind": "machinery", "set": "heldout",
             "gate": "verified", "scenario": "staleness_reopens"},
            {"id": "m-inval", "kind": "machinery", "set": "heldout",
             "gate": "checked", "scenario": "reset_invalidates_later"},
            {"id": "m-term", "kind": "machinery", "set": "heldout",
             "gate": "checked", "scenario": "escalate_is_terminal"},
        ]
        rep = self._run(mach, seeds=2)
        for cid in ("m-order", "m-stale", "m-inval", "m-term"):
            self.assertEqual(rep["cases"][cid]["passes"], [True, True], cid)

    def test_icc_present_with_enough_cases(self):
        rep = self._run(CORPUS_OK, seeds=3)
        self.assertAlmostEqual(rep["icc"], 1.0)  # deterministic corpus agrees with itself


LIVE_CASE = {
    "id": "live-1", "kind": "live", "set": "iterate", "gate": "verified",
    "adversarial": True, "path": "units/x/sketch.md",
    "prompt": "Sketch a two-line note that mentions Acme Robotics.",
    "gold": {"quiet": ["acct"],
             "require": [{"regex": "Acme Robotics"}],
             "max_words": 200},
}


class TestGoldGrading(unittest.TestCase):
    def _policies(self):
        from core import config
        with tempfile.TemporaryDirectory() as td:
            root = mkroot(Path(td))
            from core import policy
            return policy.compile_policies(config.load(root))

    def test_clean_text_meeting_requirements_passes(self):
        from core.eval import ab
        ok, reasons = ab.gold_pass("Hello from Acme Robotics.\n", LIVE_CASE, self._policies())
        self.assertTrue(ok, reasons)

    def test_leaking_gold_quiet_policy_fails(self):
        from core.eval import ab
        ok, reasons = ab.gold_pass(f"Acme Robotics id {ACCT_TOKEN}\n", LIVE_CASE,
                                   self._policies())
        self.assertFalse(ok)
        self.assertTrue(any("acct" in r for r in reasons))

    def test_missing_required_regex_fails(self):
        from core.eval import ab
        ok, reasons = ab.gold_pass("Hello from nowhere.\n", LIVE_CASE, self._policies())
        self.assertFalse(ok)

    def test_word_limit_enforced(self):
        from core.eval import ab
        long_text = "Acme Robotics " + "word " * 300
        ok, _ = ab.gold_pass(long_text, LIVE_CASE, self._policies())
        self.assertFalse(ok)


class TestPairedAB(unittest.TestCase):
    """The §13 honesty check: paired OFF/ON arms, McNemar exact."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = mkroot(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, invoke, cases=None, seeds=2, max_repairs=2):
        from core import config
        from core.eval import ab
        return ab.run_ab(self.root, config.load(self.root), cases or [LIVE_CASE],
                         seeds=seeds, invoke=invoke, max_repairs=max_repairs)

    def test_repair_loop_flips_the_pairs(self):
        # OFF arm ships the first (dirty) sketch; ON arm gets lint feedback and
        # the revision is clean → every pair is (off fail, on pass).
        def invoke(prompt):
            dirty = "revise" not in prompt.lower()
            text = (f"Acme Robotics id {ACCT_TOKEN}\n" if dirty
                    else "Acme Robotics, clean.\n")
            return {"text": text, "cost_usd": 0.01, "tokens": 10}
        cases = [dict(LIVE_CASE, id=f"live-{i}") for i in range(3)]
        rep = self._run(invoke, cases=cases, seeds=2)
        self.assertEqual(rep["pairs"], 6)
        self.assertEqual((rep["b"], rep["c"]), (0, 6))
        self.assertAlmostEqual(rep["p_value"], 0.03125, places=6)
        self.assertEqual(rep["off"]["passed"], 0)
        self.assertEqual(rep["on"]["passed"], 6)
        self.assertGreater(rep["cost_usd"], 0.0)

    def test_repairs_are_bounded(self):
        calls = []
        def invoke(prompt):
            calls.append(prompt)
            return {"text": f"always dirty {ACCT_TOKEN}\n", "cost_usd": 0.01, "tokens": 5}
        rep = self._run(invoke, seeds=1, max_repairs=2)
        # OFF: 1 call. ON: 1 sketch + at most 2 repairs = 3. Total ≤ 4.
        self.assertLessEqual(len(calls), 4)
        self.assertEqual(rep["on"]["passed"], 0)
        self.assertEqual(rep["p_value"], 1.0)  # no discordant pairs → uninformative

    def test_identical_arms_yield_p_one(self):
        def invoke(prompt):
            return {"text": "Acme Robotics, clean.\n", "cost_usd": 0.0, "tokens": 5}
        rep = self._run(invoke, seeds=2)
        self.assertEqual((rep["b"], rep["c"]), (0, 0))
        self.assertEqual(rep["p_value"], 1.0)

    def test_per_case_disagreement_reported(self):
        flip = {"n": 0}
        def invoke(prompt):
            flip["n"] += 1
            dirty = flip["n"] % 2 == 0
            return {"text": (f"x {ACCT_TOKEN}\n" if dirty else "Acme Robotics ok\n"),
                    "cost_usd": 0.0, "tokens": 5}
        rep = self._run(invoke, seeds=4, max_repairs=0)
        pc = rep["per_case"]["live-1"]
        self.assertEqual(len(pc["off"]), 4)
        self.assertIn("off_disagreement", pc)
        self.assertGreaterEqual(pc["off_disagreement"], 0.0)
        self.assertLessEqual(pc["off_disagreement"], 0.5)


class TestTauCalibration(unittest.TestCase):
    def test_threshold_separates_error_bands(self):
        from core.eval import ab
        # low-disagreement points shipped fine; high-disagreement points erred
        points = [(0.0, 0), (0.0, 0), (0.2, 0), (0.4, 1), (0.4, 1), (0.5, 1)]
        tau, table = ab.calibrate_tau(points)
        self.assertGreater(tau, 0.2)
        self.assertLessEqual(tau, 0.4)

    def test_no_points_returns_none(self):
        from core.eval import ab
        self.assertEqual(ab.calibrate_tau([]), (None, []))


class TestEvalCli(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = mkroot(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def _cli(self, *args):
        import subprocess
        cli = Path(__file__).resolve().parents[1] / "eval" / "cli.py"
        return subprocess.run([sys.executable, str(cli), *args],
                              capture_output=True, text=True)

    def _write_corpus(self, cases):
        import yaml
        cdir = self.root / "harness" / "eval" / "corpus"
        cdir.mkdir(parents=True)
        (cdir / "cases.yaml").write_text(yaml.safe_dump(cases))

    def test_run_prints_ci_icc_histogram_and_exits_zero(self):
        self._write_corpus(CORPUS_OK)
        p = self._cli("run", "--root", str(self.root), "--seeds", "3")
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)
        self.assertIn("CI", p.stdout)
        self.assertIn("ICC", p.stdout)
        self.assertIn("histogram", p.stdout)
        # F17: the deterministic ICC is necessarily 1.000, the output must
        # say so, or a reader mistakes it for agent-level reliability.
        self.assertIn("determinism check", p.stdout)

    def test_run_regression_exits_one(self):
        miss = {"id": "det-miss", "kind": "detection", "set": "heldout",
                "gate": "verified", "path": "units/x/sketch.md",
                "artifact": "nothing here\n", "expect": {"fire": ["acct"], "quiet": []}}
        self._write_corpus(CORPUS_OK + [miss])
        p = self._cli("run", "--root", str(self.root), "--seeds", "2")
        self.assertEqual(p.returncode, 1)
        self.assertIn("det-miss", p.stdout)

    def test_missing_corpus_says_so(self):
        p = self._cli("run", "--root", str(self.root))
        self.assertEqual(p.returncode, 1)
        self.assertIn("corpus", (p.stdout + p.stderr).lower())

    def test_ab_refuses_existing_out_without_force(self):
        # A committed A/B report is pre-registered EVIDENCE (docs/DESIGN.md);
        # `make eval-ab` must not clobber it casually. The refusal fires
        # before any live-case filtering or worker spend.
        self._write_corpus(CORPUS_OK)
        out = self.root / "ab_report.json"
        out.write_text('{"evidence": true}\n', encoding="utf-8")
        p = self._cli("ab", "--root", str(self.root), "--out", str(out))
        self.assertEqual(p.returncode, 1)
        self.assertIn("pre-register", (p.stdout + p.stderr).lower())
        self.assertEqual(out.read_text(encoding="utf-8"),
                         '{"evidence": true}\n', "evidence file must be untouched")


class TestRefusedOutcome(unittest.TestCase):
    """F13: non-convergence is a third state, production refuses the gate,
    it does not ship. The A/B must record it, not tariff it away."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = mkroot(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, invoke, **kw):
        from core import config
        from core.eval import ab
        return ab.run_ab(self.root, config.load(self.root), [LIVE_CASE],
                         seeds=1, invoke=invoke, **kw)

    def test_unrepairable_violation_is_recorded_as_refused(self):
        def invoke(prompt):  # every sketch and every repair keeps the violation
            return {"text": f"Acme Robotics id {ACCT_TOKEN}\n",
                    "cost_usd": 0.01, "tokens": 5}
        rep = self._run(invoke, max_repairs=2)
        self.assertEqual(rep["refused"], 1,
                         "fatal findings surviving the final repair = refused")
        pc = rep["per_case"][LIVE_CASE["id"]]
        self.assertEqual(pc["on_outcomes"], ["refused"])

    def test_converged_trial_is_pass(self):
        def invoke(prompt):
            dirty = "revise" not in prompt.lower()
            return {"text": (f"Acme Robotics id {ACCT_TOKEN}\n" if dirty
                             else "Acme Robotics, clean.\n"),
                    "cost_usd": 0.01, "tokens": 5}
        rep = self._run(invoke)
        self.assertEqual(rep["refused"], 0)
        self.assertEqual(rep["per_case"][LIVE_CASE["id"]]["on_outcomes"], ["pass"])

    def test_gold_failure_without_findings_is_fail_not_refused(self):
        def invoke(prompt):  # no policy violation, but misses the gold require
            return {"text": "Hello from nowhere.\n", "cost_usd": 0.0, "tokens": 5}
        rep = self._run(invoke)
        self.assertEqual(rep["refused"], 0)
        self.assertEqual(rep["per_case"][LIVE_CASE["id"]]["on_outcomes"], ["fail"])


class TestRepairPromptHierarchy(unittest.TestCase):
    """F13(b): the repair prompt's instruction hierarchy must be explicit.
    policy beats task; obfuscation is named and forbidden."""

    def test_repair_prompt_states_policy_beats_task(self):
        from core.eval import ab
        prompt = ab._repair_prompt("write the memo", "sketch", ["[acct] line 1"])
        low = prompt.lower()
        self.assertIn("outranks", low)
        self.assertIn("obfuscat", low)


class TestAbArtifacts(unittest.TestCase):
    """F15: per-trial artifacts persist, a failing trial must be auditable.
    and progress is flushed per case so a hung run is distinguishable."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = mkroot(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_out_dir_gets_sketchs_and_meta(self):
        from core import config
        from core.eval import ab

        def invoke(prompt):
            return {"text": f"always dirty {ACCT_TOKEN}\n", "cost_usd": 0.01,
                    "tokens": 5}
        out = Path(self._tmp.name) / "run1"
        ab.run_ab(self.root, config.load(self.root), [LIVE_CASE], seeds=1,
                  invoke=invoke, max_repairs=1, out_dir=out)
        case_dir = out / LIVE_CASE["id"]
        self.assertTrue((case_dir / "trial0-off.md").exists())
        self.assertTrue((case_dir / "trial0-on.md").exists())
        meta = json.loads((case_dir / "trial0.json").read_text())
        self.assertEqual(meta["on_outcome"], "refused")
        self.assertEqual(meta["repairs"], 1)
        self.assertTrue(meta["on_gold_reasons"])

    def test_progress_lines_flush_per_case(self):
        import io
        from contextlib import redirect_stdout
        from core import config
        from core.eval import ab

        def invoke(prompt):
            return {"text": "Acme Robotics, clean.\n", "cost_usd": 0.0,
                    "tokens": 5}
        buf = io.StringIO()
        with redirect_stdout(buf):
            ab.run_ab(self.root, config.load(self.root), [LIVE_CASE], seeds=1,
                      invoke=invoke, progress=True)
        self.assertIn(LIVE_CASE["id"], buf.getvalue(),
                      "no per-case progress line, a hung run is invisible")


class TestLiveIcc(unittest.TestCase):
    """F17: the live ICC(1) comes from the stochastic per-case rows, the
    deterministic corpus ICC is a determinism check, not agent reliability."""

    def test_report_carries_live_icc_with_enough_cases(self):
        from core import config
        from core.eval import ab
        flip = {"n": 0}

        def invoke(prompt):
            flip["n"] += 1
            dirty = flip["n"] % 3 == 0
            return {"text": (f"x {ACCT_TOKEN}\n" if dirty
                             else "Acme Robotics ok\n"),
                    "cost_usd": 0.0, "tokens": 5}
        with tempfile.TemporaryDirectory() as td:
            root = mkroot(Path(td))
            cases = [dict(LIVE_CASE, id=f"live-{i}") for i in range(3)]
            rep = ab.run_ab(root, config.load(root), cases, seeds=4,
                            invoke=invoke, max_repairs=0)
        self.assertIn("live_icc", rep)
        self.assertIsNotNone(rep["live_icc"])
        self.assertLessEqual(abs(rep["live_icc"]), 1.0)


if __name__ == "__main__":
    unittest.main()
