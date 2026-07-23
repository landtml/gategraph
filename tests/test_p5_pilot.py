"""P5 exit tests: the pilot "consumer-A" (plan §17 P5).

A stranger project stamped from the template must:
  - walk composed -> linted -> reviewed end-to-end with ZERO hand-edits to
    harness/core (the `reviewed` gate is earned from a schema-valid,
    sha256-anchored review.json per §11, wired entirely in the adapter layer);
  - run a bounded 10-iteration --mock-workers loop after flipping loop.enabled
    to the shipped toy oracle;
  - survive the gatebench update sync path under local core drift (§15 Risk 3):
    a template release must never clobber a local hotfix (.fhnew + report),
    core-drift lint must nag, and gatebench upstream must close the loop.

The pilot's project-side layer lives in tests/pilot/ (cross_review.py,
review_gate.py, wire.py); the same assets stamp the real pilot project.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import yaml

from test_fh_init import run_init

TEMPLATE = Path(__file__).resolve().parent.parent
PILOT = Path(__file__).resolve().parent / "pilot"

ANSWERS = {
    "project": "consumer-A",
    "artifact_noun": "note",
    "unit_dir": "notes",
    "gates": ["composed", "linted", "reviewed"],
    "hashed_gates": ["linted", "reviewed"],   # both tool-set + staleness-tracked
    "seed_deny": True,                        # the one deny-regex (no-acct)
    "loop_enabled": False,                    # flipped later by the project
}
ACCT_TOKEN = "010190" + "-1234"  # assembled so this file never stores a literal

GOOD_NOTE = """# w1: Weekly note: harness pilot

## Findings
- The freshly stamped template walks its own gate chain.
- Gates advance only on evidence recorded in the unit manifest.

## Sources
- source: harness/core/PROTOCOL.md
- source: plan.md §17 (P5 row)
"""

BAD_NOTE = """# w2: stub

TODO: actually write this note
"""


def wire_pilot(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(PILOT / "wire.py"), str(root)],
                          capture_output=True, text=True)


class TestCrossReviewScript(unittest.TestCase):
    """The blinded cross-model reviewer seam, run bare (no stamped project):
    artifact path + rubric path in, schema-valid review.json out."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.rubric = TEMPLATE / "seed" / "review_rubric.md"

    def tearDown(self):
        self._tmp.cleanup()

    def review(self, text: str) -> tuple[subprocess.CompletedProcess, Path]:
        artifact = self.dir / "note.md"
        artifact.write_text(text)
        out = self.dir / "review.json"
        proc = subprocess.run(
            [sys.executable, str(PILOT / "cross_review.py"),
             "--artifact", str(artifact), "--rubric", str(self.rubric),
             "--out", str(out)],
            capture_output=True, text=True)
        return proc, out

    def test_clean_note_passes_with_anchored_sha(self):
        proc, out = self.review(GOOD_NOTE)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        review = json.loads(out.read_text())
        self.assertEqual(sorted(review), ["artifact_sha256", "findings", "verdict"])
        self.assertEqual(review["verdict"], "pass")
        import hashlib
        expect = hashlib.sha256((self.dir / "note.md").read_bytes()).hexdigest()
        self.assertEqual(review["artifact_sha256"], expect)
        self.assertIsInstance(review["findings"], list)

    def test_placeholder_note_fails_with_cited_evidence(self):
        proc, out = self.review(BAD_NOTE)
        self.assertEqual(proc.returncode, 0,
                         "a fail verdict is still a successful review run")
        review = json.loads(out.read_text())
        self.assertEqual(review["verdict"], "fail")
        fatal = [f for f in review["findings"] if f["severity"] == "fatal"]
        self.assertTrue(fatal, "fail verdict must carry a fatal finding")
        self.assertTrue(any("TODO" in f["evidence"] for f in fatal),
                        f"finding must cite the offending span: {fatal}")


class TestPilotLifecycle(unittest.TestCase):
    """consumer-A stamped from the REAL template, wired project-side, then
    driven through the full interactive lifecycle + the bounded loop."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / "consumer-A"
        proc = run_init(cls.root, answers=ANSWERS)
        if proc.returncode != 0:
            raise RuntimeError(f"init failed:\n{proc.stdout}\n{proc.stderr}")
        wired = wire_pilot(cls.root)
        if wired.returncode != 0:
            raise RuntimeError(f"wire failed:\n{wired.stdout}\n{wired.stderr}")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    # ---------------------------------------------------------------- helpers

    def cli(self, script, *args):
        return subprocess.run(
            [sys.executable, str(self.root / "harness/core" / script), *args,
             "--root", str(self.root)],
            capture_output=True, text=True)

    def check_script(self, script, *args):
        return subprocess.run(
            [sys.executable, str(self.root / "harness/adapter/checks" / script),
             *args, "--root", str(self.root)],
            capture_output=True, text=True)

    def hook(self, script, event):
        env = {**os.environ, "CLAUDE_PROJECT_DIR": str(self.root)}
        return subprocess.run(
            [sys.executable, str(self.root / "harness/core/hooks" / script)],
            input=json.dumps(event), capture_output=True, text=True, env=env)

    def cross_review(self, slug="w1"):
        return subprocess.run(
            [sys.executable, str(self.root / "harness/adapter/checks/cross_review.py"),
             "--artifact", str(self.root / f"notes/{slug}/note.md"),
             "--rubric", str(self.root / "harness/adapter/review_rubric.md"),
             "--out", str(self.root / f"notes/{slug}/review.json")],
            capture_output=True, text=True)

    def manifest(self, slug="w1"):
        return yaml.safe_load((self.root / f"notes/{slug}/manifest.yaml").read_text())

    # ---------------------------------------------------------------- tests

    def test_01_wired_project_is_gating_clean(self):
        # wiring added a check with `enforces: reviewed-evidence` + the CLAUDE.md
        # annotation; rule-coverage (gating) must hold in both directions
        r = subprocess.run(
            [sys.executable, "harness/core/harness_lint.py", "--tier", "gating"],
            cwd=self.root, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        adapter = yaml.safe_load(
            (self.root / "harness/adapter/adapter.yaml").read_text())
        self.assertTrue(adapter["reviewer"]["cross_model"]["enabled"])
        self.assertIn("reviewed", adapter["workflow"]["tool_set_gates"])
        self.assertIn("review_gate.py",
                      adapter["workflow"]["gate_commands"]["reviewed"])

    def test_02_start_deny_and_composed(self):
        r = self.cli("gate.py", "start", "--unit", "w1", "--session-id", "P5")
        self.assertEqual(r.returncode, 0, r.stderr)

        # the one deny-regex: pretool guard blocks ACCT content into the unit
        ev = {"session_id": "P5", "hook_event_name": "PreToolUse",
              "tool_name": "Write",
              "tool_input": {"file_path": str(self.root / "notes/w1/note.md"),
                             "content": "id " + ACCT_TOKEN + "\n"}}
        out = json.loads(self.hook("pretool_guard.py", ev).stdout)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

        (self.root / "notes/w1/note.md").write_text(GOOD_NOTE)
        r = self.cli("gate.py", "approve", "composed", "--unit", "w1")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_03_linted_is_tool_set(self):
        r = self.cli("gate.py", "approve", "linted", "--unit", "w1")
        self.assertEqual(r.returncode, 1, "tool-set gate must refuse manual approve")
        r = self.cli("lint.py", "--unit", "w1", "--set-gate", "linted")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_04_reviewed_gate_is_a_hard_wall(self):
        # manual approve refused (tool-set)
        r = self.cli("gate.py", "approve", "reviewed", "--unit", "w1")
        self.assertEqual(r.returncode, 1)

        # no review.json yet -> the producing check refuses, names the reviewer
        r = self.check_script("review_gate.py", "--unit", "w1")
        self.assertEqual(r.returncode, 1, "must refuse without review.json")
        self.assertIn("cross_review", r.stdout + r.stderr)
        self.assertNotIn("reviewed", self.manifest().get("gates", {}))

        # a fail-verdict review must NOT earn the gate
        review_path = self.root / "notes/w1/review.json"
        good_review = None
        r = self.cross_review()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        good_review = review_path.read_text()
        bad = json.loads(good_review)
        bad["verdict"] = "fail"
        review_path.write_text(json.dumps(bad))
        r = self.check_script("review_gate.py", "--unit", "w1")
        self.assertEqual(r.returncode, 1, "fail verdict must not set the gate")

        # a tampered sha must NOT earn the gate
        bad = json.loads(good_review)
        bad["artifact_sha256"] = "0" * 64
        review_path.write_text(json.dumps(bad))
        r = self.check_script("review_gate.py", "--unit", "w1")
        self.assertEqual(r.returncode, 1, "sha mismatch must not set the gate")

        # the genuine pass verdict earns it, with sha256 evidence recorded
        review_path.write_text(good_review)
        r = self.check_script("review_gate.py", "--unit", "w1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        gates = self.manifest()["gates"]
        self.assertIn("reviewed", gates)
        self.assertIn("note.md", gates["reviewed"]["sha256"])

    def test_05_editing_the_note_reopens_review(self):
        note = self.root / "notes/w1/note.md"
        note.write_text(GOOD_NOTE + "\n- late addition after review\n")

        r = self.cli("gate.py", "status", "--unit", "w1")
        self.assertIn("STALE", r.stdout)

        # defense in depth: any lint scan now fails the review-fresh check
        r = self.cli("lint.py", "--unit", "w1")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("review-fresh", r.stdout + r.stderr)

        # re-earn in order: fresh review -> linted -> reviewed
        self.assertEqual(self.cross_review().returncode, 0)
        r = self.cli("lint.py", "--unit", "w1", "--set-gate", "linted")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.check_script("review_gate.py", "--unit", "w1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = self.cli("gate.py", "status", "--unit", "w1")
        self.assertNotIn("STALE", r.stdout)
        self.assertIn("complete", r.stdout)

    def test_06_stop_gate_demands_handoff_then_releases(self):
        r = self.hook("stop_gate.py", {"session_id": "P5",
                                       "hook_event_name": "Stop"})
        block = json.loads(r.stdout)
        self.assertEqual(block["decision"], "block")
        self.assertIn("handoff", block["reason"].lower())

        time.sleep(0.02)  # handoff mtime must beat the session marker's
        (self.root / "HANDOFF.md").write_text(
            "# HANDOFF\n\n## Now\nw1 reviewed and complete\n\n## Next\nstart w2\n")
        r = self.hook("stop_gate.py", {"session_id": "P5",
                                       "hook_event_name": "Stop"})
        self.assertEqual(r.stdout.strip(), "", "consolidated unit must release stop")

    def test_07_bounded_loop_with_shipped_toy_oracle(self):
        adapter_path = self.root / "harness/adapter/adapter.yaml"
        adapter = yaml.safe_load(adapter_path.read_text())
        adapter["loop"]["enabled"] = True
        adapter["loop"]["oracle"] = "core.loop.toy_oracle:ToyOracle"
        adapter["loop"]["backstops"]["max_iterations"] = 10
        adapter["loop"]["backstops"]["cost_budget_usd"] = 1.0
        adapter_path.write_text(yaml.safe_dump(adapter, sort_keys=False))

        cli = self.root / "harness/core/loop/cli.py"
        r = subprocess.run([sys.executable, str(cli), "validate"],
                           cwd=self.root, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        r = subprocess.run([sys.executable, str(cli), "run", "--mock-workers"],
                           cwd=self.root, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        # registry.jsonl is an append-only EVENT log: each iteration records a
        # `proposal` card and a `verdict` card (kind-tagged lines)
        registry = self.root / "harness/state/loop/registry.jsonl"
        events = [json.loads(l) for l in
                  registry.read_text().splitlines() if l.strip()]
        proposals = [e for e in events if e.get("kind") == "proposal"]
        verdicts = [e for e in events if e.get("kind") == "verdict"]
        self.assertEqual(len(proposals), 10, "backstop must bound the run at 10")
        self.assertEqual(len(verdicts), 10, "every iteration must reach a verdict")
        kinds = {v["verdict"] for v in verdicts}
        self.assertEqual(kinds, {"GRADUATE"},
                         f"toy oracle default is all-pass: {kinds}")
        self.assertTrue((self.root / "harness/state/loop/STATUS.md").exists())
        self.assertFalse((self.root / "harness/state/loop/loop.pid").exists(),
                         "pidfile must be released after a clean run")

    def test_08_zero_hand_edits_to_core_and_check_green(self):
        # THE §17 exit criterion, in checkable form: after the entire lifecycle
        # and the loop, the stamped core is bit-identical to the template
        r = subprocess.run(
            [sys.executable, str(TEMPLATE / "gatebench.py"), "diff", str(self.root)],
            capture_output=True, text=True)
        self.assertEqual(r.returncode, 0,
                         f"core drifted during the pilot:\n{r.stdout}{r.stderr}")
        self.assertIn("pristine", r.stdout)

        # the §11 "done" chain still holds with the loop enabled
        r = subprocess.run(
            [sys.executable, "harness/core/harness_lint.py", "--tier", "gating"],
            cwd=self.root, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        r = subprocess.run(
            [sys.executable, "harness/core/loop/cli.py", "validate"],
            cwd=self.root, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class TestPilotSyncStress(unittest.TestCase):
    """§15 Risk 3 on the pilot itself: template release + local core hotfix.
    Runs against a scratch COPY of the template (gatebench upstream writes into the
    template); the real template repo is never touched."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.template = base / "template"
        self.template.mkdir()
        for item in ("gatebench.py", "VERSION", "core", "seed"):
            src = TEMPLATE / item
            if src.is_dir():
                shutil.copytree(src, self.template / item,
                                ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy2(src, self.template / item)
        self.project = base / "consumer-A"
        answers = base / "answers.yaml"
        answers.write_text(yaml.safe_dump(ANSWERS))
        r = self.fh("init", str(self.project), "--answers", str(answers))
        assert r.returncode == 0, r.stdout + r.stderr

    def tearDown(self):
        self._tmp.cleanup()

    def fh(self, *args):
        return subprocess.run(
            [sys.executable, str(self.template / "gatebench.py"), *args],
            capture_output=True, text=True)

    def lint_machine(self, tier="warning"):
        r = subprocess.run(
            [sys.executable, "harness/core/harness_lint.py",
             "--tier", tier, "--machine"],
            cwd=self.project, capture_output=True, text=True)
        return r.stdout

    def release_and_hotfix(self):
        """Template ships a release while the project carries a core hotfix."""
        hotfix = self.project / "harness/core/gate.py"
        hotfix.write_text(hotfix.read_text()
                          + "\n# PILOT HOTFIX: battle-tested in consumer-A\n")
        proto = self.template / "core/PROTOCOL.md"
        proto.write_text(proto.read_text() + "\n## Release note\n\nUpstream.\n")
        (self.template / "VERSION").write_text("0.2.0-test\n")
        return hotfix

    def test_update_delivers_release_but_never_clobbers_hotfix(self):
        hotfix = self.release_and_hotfix()
        r = self.fh("update", str(self.project))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

        stamped_proto = self.project / "harness/core/PROTOCOL.md"
        self.assertIn("Release note", stamped_proto.read_text(),
                      "pristine file must receive the release")
        self.assertIn("PILOT HOTFIX", hotfix.read_text(),
                      "modified file must never be clobbered")
        fhnew = hotfix.with_name("gate.py.fhnew")
        self.assertTrue(fhnew.exists(), ".fhnew sidecar missing")
        self.assertNotIn("PILOT HOTFIX", fhnew.read_text())
        self.assertIn("gate.py", r.stdout)            # drift report names it
        self.assertIn("DRIFT", r.stdout)
        manifest = json.loads(
            (self.project / "harness/.fh/MANIFEST.json").read_text())
        self.assertEqual(manifest["version"], "0.2.0-test")

        # core-drift lint nags about exactly the hotfixed file
        out = self.lint_machine()
        drift = [l for l in out.splitlines() if l.startswith("core-drift")]
        self.assertTrue(any("gate.py" in l for l in drift), out)

        r = self.fh("diff", str(self.project))
        self.assertEqual(r.returncode, 1, "drift present -> diff exits 1")

    def test_upstream_promotes_hotfix_and_closes_the_loop(self):
        hotfix = self.release_and_hotfix()
        self.fh("update", str(self.project))

        r = self.fh("upstream", str(self.project), "harness/core/gate.py")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("PILOT HOTFIX",
                      (self.template / "core/gate.py").read_text(),
                      "hotfix must land in the template")

        # owner resolves the sidecar after promoting
        hotfix.with_name("gate.py.fhnew").unlink()

        r = self.fh("diff", str(self.project))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = self.lint_machine()
        self.assertFalse([l for l in out.splitlines()
                          if l.startswith("core-drift")],
                         f"core-drift must be clean after upstream:\n{out}")

        # the promoted template still stamps a live project (self-check green)
        fresh = self.project.parent / "fresh-after-promote"
        answers = self.project.parent / "answers2.yaml"
        answers.write_text(yaml.safe_dump(dict(ANSWERS, project="fresh")))
        r = self.fh("init", str(fresh), "--answers", str(answers))
        self.assertEqual(r.returncode, 0,
                         f"promoted template must still stamp:\n{r.stdout}{r.stderr}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
