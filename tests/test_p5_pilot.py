"""P5 exit tests: the pilot "consumer-A" (plan §17 P5), on the schema-2 layout.

A stranger project stamped from the template must:
  - walk composed -> linted -> reviewed end-to-end with ZERO hand-edits to harness/core.
    `composed` is an approval gate on the token channel (a person answers it with the one-time
    token). `linted` is a check gate (policy scan). `reviewed` is earned by `gate attempt`
    running two checks: the policy scan and review_gate.py, which accepts only a fresh,
    sha256-anchored review.json with verdict `pass`;
  - run a bounded 10-iteration toy search (the search CLI that the loop shim maps to);
  - survive the gategraph update sync path under local core drift (§15 Risk 3): a template
    release must never clobber a local hotfix (.fhnew + report), `gategraph diff` must name the
    drifted file, and `gategraph upstream` must close the loop.

The pilot's project-side layer lives in tests/pilot/ (cross_review.py, review_gate.py, wire.py).
A gate passes only through `gate attempt` (checks) or `gate proceed --token` (approvals); a direct
set is refused (bug B01).
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
from test_stamp_v2 import require_template_venv, seed_venv

TEMPLATE = Path(__file__).resolve().parent.parent
PILOT = Path(__file__).resolve().parent / "pilot"
UNIT = "w1"
PROJECT = "consumer-A"
ACCT_TOKEN = "010190" + "-1234"  # assembled so this file never stores a literal

ANSWERS = {
    "project": PROJECT,
    "artifact_noun": "note",
    "unit_dir": "notes",
    "gates": ["composed", "linted", "reviewed"],
    "approval_gates": ["composed"],        # a person answers this gate with the token
    "approval_channel": "token",
    "seed_deny": True,                    # the one deny-regex (no-acct)
}

GOOD_NOTE = """# w1: Weekly note: harness pilot

## Findings
- The freshly stamped template walks its own gate chain.
- Gates advance only on evidence recorded in the unit manifest.

## Sources
- source: harness/core/PROTOCOL.md
- source: harness/adapter/adapter.yaml
"""

BAD_NOTE = """# w2: stub

TODO: actually write this note
"""

TOKEN_PREFIX = "approval token (shown once, not stored): "


def wire_pilot(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(PILOT / "wire.py"), str(root)],
                          capture_output=True, text=True)


def token_in(text: str) -> str:
    for line in text.splitlines():
        if line.strip().startswith(TOKEN_PREFIX.strip()):
            return line.split(":", 1)[1].strip()
    raise AssertionError(f"no approval token printed:\n{text}")


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
    """consumer-A stamped from the REAL template, wired project-side, then driven through the
    gate lifecycle and the bounded search."""

    @classmethod
    def setUpClass(cls):
        require_template_venv()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / PROJECT
        proc = run_init(cls.root, answers=ANSWERS)
        if proc.returncode != 0:
            raise RuntimeError(f"init failed:\n{proc.stdout}\n{proc.stderr}")
        wired = wire_pilot(cls.root)
        if wired.returncode != 0:
            raise RuntimeError(f"wire failed:\n{wired.stdout}\n{wired.stderr}")
        cls.py = seed_venv(cls.root)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    # ---------------------------------------------------------------- helpers

    def gate(self, *args):
        return subprocess.run([str(self.py), "harness/core/cli/gate.py", *args],
                              cwd=self.root, capture_output=True, text=True)

    def check_script(self, script, *args):
        return subprocess.run([str(self.py), f"harness/adapter/checks/{script}", *args],
                              cwd=self.root, capture_output=True, text=True)

    def legacy(self, script, *args):
        return subprocess.run([sys.executable, str(self.root / "harness/core" / script), *args],
                              cwd=self.root, capture_output=True, text=True)

    def hook(self, script, event):
        env = {**os.environ, "CLAUDE_PROJECT_DIR": str(self.root)}
        return subprocess.run(
            [sys.executable, str(self.root / "harness/core/hosts/shims" / script)],
            input=json.dumps(event), capture_output=True, text=True, env=env)

    def cross_review(self, slug=UNIT):
        return subprocess.run(
            [sys.executable, str(self.root / "harness/adapter/checks/cross_review.py"),
             "--artifact", str(self.root / f"notes/{slug}/note.md"),
             "--rubric", str(self.root / "harness/adapter/review_rubric.md"),
             "--out", str(self.root / f"notes/{slug}/review.json")],
            capture_output=True, text=True)

    def manifest_text(self, slug=UNIT):
        return (self.root / f"notes/{slug}/manifest.yaml").read_text()

    def status(self, slug=UNIT):
        return self.gate("status", "--unit", slug).stdout

    def walk_composed(self, slug=UNIT):
        """`composed` is an approval: raise it with `attempt`, answer it with the token."""
        raised = self.gate("attempt", "composed", "--unit", slug)
        self.assertEqual(raised.returncode, 0, raised.stdout + raised.stderr)
        tok = token_in(raised.stdout)
        answered = self.gate("proceed", "--unit", slug, "--decision", "approve",
                             "--by", "tester", f"--token={tok}")
        self.assertEqual(answered.returncode, 0, answered.stdout + answered.stderr)
        return tok

    # ---------------------------------------------------------------- tests

    def test_01_wired_project_is_gating_clean(self):
        # the review check and the reviewed gate are in the adapter; selflint (gating) is clean
        r = subprocess.run([str(self.py), "-m", "core.kernel.selflint", "--tier", "gating",
                            "--root", "."],
                           cwd=self.root, capture_output=True, text=True,
                           env={**os.environ, "PYTHONPATH": "harness"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        adapter = yaml.safe_load((self.root / "harness/adapter/adapter.yaml").read_text())
        self.assertEqual(adapter["schema"], 2)
        self.assertNotIn("workflow", adapter, "schema 2 has no workflow block")
        reviewed = next(g for g in adapter["gates"] if g["id"] == "reviewed")
        self.assertEqual(reviewed["checks"], ["policy", "review"])
        review = next(c for c in adapter["checks"] if c["id"] == "review")
        self.assertIn("review_gate.py", review["command"])
        self.assertEqual(review["severity"], "fatal")
        self.assertTrue((self.root / "harness/adapter/checks/cross_review.py").is_file())

        # wiring is idempotent: a second run leaves a single review check
        self.assertEqual(wire_pilot(self.root).returncode, 0)
        adapter = yaml.safe_load((self.root / "harness/adapter/adapter.yaml").read_text())
        self.assertEqual([c["id"] for c in adapter["checks"]].count("review"), 1)

    def test_02_start_deny_and_composed(self):
        r = self.gate("start", "--unit", UNIT, "--session-id", "P5")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"gate attempt composed --unit {UNIT}", self.gate(
            "pending", "--unit", UNIT).stdout)

        # a unit waiting at its work request is in_progress: the stop holds the armed session on
        # the stale handoff and on the unit, and the reason names the gate command (plan 1.16)
        block = json.loads(self.hook("stop_gate.py", {"session_id": "P5",
                                                      "hook_event_name": "Stop"}).stdout)
        self.assertEqual(block["decision"], "block")
        self.assertIn("handoff", block["reason"].lower())
        self.assertIn(f"gate attempt composed --unit {UNIT}", block["reason"])

        # the one deny-regex: pretool guard blocks ACCT content into the unit
        ev = {"session_id": "P5", "hook_event_name": "PreToolUse",
              "tool_name": "Write",
              "tool_input": {"file_path": str(self.root / f"notes/{UNIT}/note.md"),
                             "content": "id " + ACCT_TOKEN + "\n"}}
        out = json.loads(self.hook("pretool_guard.py", ev).stdout)
        self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")

        (self.root / f"notes/{UNIT}/note.md").write_text(GOOD_NOTE)
        raised = self.gate("attempt", "composed", "--unit", UNIT)
        self.assertEqual(raised.returncode, 0, raised.stderr)
        tok = token_in(raised.stdout)

        # a unit waiting at its approval is paused (plan 1.16): with a current handoff the stop is
        # allowed. The original handoff is put back with its mtime, so the stale-handoff stop checks
        # of test_04 and test_06 still hold.
        handoff = self.root / "HANDOFF.md"
        original, before = handoff.read_bytes(), handoff.stat()
        time.sleep(0.02)
        handoff.write_text("# HANDOFF\n\n## Now\nw1 waits at its approval\n\n## Next\nanswer it\n")
        r = self.hook("stop_gate.py", {"session_id": "P5", "hook_event_name": "Stop"})
        self.assertEqual(r.stdout.strip(), "", "a unit waiting for an approval must not hold stop")
        handoff.write_bytes(original)
        os.utime(handoff, ns=(before.st_atime_ns, before.st_mtime_ns))

        # the approval has no bypass: no token, and a wrong token, are both refused
        r = self.gate("proceed", "--unit", UNIT, "--decision", "approve", "--by", "tester")
        self.assertEqual(r.returncode, 1)
        self.assertIn("token", r.stderr)
        r = self.gate("proceed", "--unit", UNIT, "--decision", "approve", "--by", "tester",
                      "--token=0000")
        self.assertEqual(r.returncode, 1)
        self.assertIn("token", r.stderr)

        r = self.gate("proceed", "--unit", UNIT, "--decision", "approve", "--by", "tester",
                      f"--token={tok}")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("gates passed: composed", self.status())

    def test_03_linted_is_checked_and_a_direct_set_is_refused(self):
        # B01: a gate passes only through its checks or an approval. A direct set is refused:
        # the legacy entry points refuse a schema-2 project, no verb takes a gate name, a
        # refused proceed changes nothing, and the manifest carries no sha256 map.
        r = self.legacy("gate.py", "approve", "linted", "--unit", UNIT)
        self.assertNotEqual(r.returncode, 0, "legacy approve must be refused (B01)")
        r = self.legacy("lint.py", "--unit", UNIT, "--set-gate", "linted")
        self.assertNotEqual(r.returncode, 0, "legacy --set-gate must be refused (B01)")
        r = self.gate("approve", "linted", "--unit", UNIT)
        self.assertEqual(r.returncode, 2, "gate CLI has no approve verb (B01)")
        r = self.gate("proceed", "--unit", UNIT, "--decision", "approve", "--by", "tester",
                      "--token=0000")
        self.assertEqual(r.returncode, 1, "linted waits for work, not an approval (B01)")
        self.assertNotIn("linted", self.status().split("gates passed:")[1].split("\n")[0])
        self.assertNotIn("sha256", self.manifest_text(), "no set may write a hash map (B01)")

        # the earned path: the policy check runs under `gate attempt`
        r = self.gate("attempt", "linted", "--unit", UNIT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("gates passed: composed, linted", self.status())

    def test_04_reviewed_gate_is_a_hard_wall(self):
        # no review.json yet: the attempt fails its review check, exits 1 with the finding, and the
        # gate stays open with its work request re-issued
        r = self.gate("attempt", "reviewed", "--unit", UNIT)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("cross_review", r.stdout)
        self.assertIn(f"next: gate attempt reviewed --unit {UNIT}", r.stdout)
        status = self.status()
        self.assertIn("gates passed: composed, linted", status)
        self.assertIn("waiting at work for gate reviewed", status)
        self.assertNotIn("reviewed", status.split("gates passed:")[1].split("\n")[0])
        block = json.loads(self.hook("stop_gate.py", {"session_id": "P5",
                                                      "hook_event_name": "Stop"}).stdout)
        self.assertIn(f"gate attempt reviewed --unit {UNIT}", block["reason"])

        # the check itself names the reviewer when there is no review
        r = self.check_script("review_gate.py", "--unit", UNIT)
        self.assertEqual(r.returncode, 1, "must refuse without review.json")
        self.assertIn("cross_review", r.stdout + r.stderr)

        # a fail-verdict review must NOT earn the gate
        review_path = self.root / f"notes/{UNIT}/review.json"
        self.assertEqual(self.cross_review().returncode, 0)
        good_review = review_path.read_text()
        bad = json.loads(good_review)
        bad["verdict"] = "fail"
        review_path.write_text(json.dumps(bad))
        r = self.check_script("review_gate.py", "--unit", UNIT)
        self.assertEqual(r.returncode, 1, "fail verdict must not pass the check")

        # a tampered sha must NOT earn the gate
        bad = json.loads(good_review)
        bad["artifact_sha256"] = "0" * 64
        review_path.write_text(json.dumps(bad))
        r = self.check_script("review_gate.py", "--unit", UNIT)
        self.assertEqual(r.returncode, 1, "sha mismatch must not pass the check")

        # the genuine pass verdict passes the check; the gate is still earned only by attempt
        review_path.write_text(good_review)
        r = self.check_script("review_gate.py", "--unit", UNIT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("waiting at work for gate reviewed", self.status())

    def test_05_editing_the_note_reopens_review(self):
        note = self.root / f"notes/{UNIT}/note.md"
        note.write_text(GOOD_NOTE + "\n- late addition after review\n")

        # the review no longer matches the note: the check refuses, and verify sees the change
        r = self.check_script("review_gate.py", "--unit", UNIT)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("artifact changed", r.stdout + r.stderr)
        r = self.gate("verify", "--unit", UNIT)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("stale", r.stdout)

        # attempting the reviewed gate on the stale review fails and reopens the chain: the
        # earlier gates went stale with the note, so the unit waits at `composed` again
        r = self.gate("attempt", "reviewed", "--unit", UNIT)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("artifact changed", r.stdout)
        status = self.status()
        self.assertIn("stale: composed, linted", status)
        self.assertIn("waiting at work for gate composed", status)

        # re-earn in order: approval (fresh token), linted, a fresh review, reviewed
        self.walk_composed()
        self.assertEqual(self.gate("attempt", "linted", "--unit", UNIT).returncode, 0)
        self.assertEqual(self.cross_review().returncode, 0)
        r = self.gate("attempt", "reviewed", "--unit", UNIT)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        status = self.status()
        self.assertIn("complete", status)
        self.assertIn("gates passed: composed, linted, reviewed", status)
        self.assertEqual(self.gate("verify", "--unit", UNIT).returncode, 0)

    def test_06_stop_gate_demands_handoff_then_releases(self):
        r = self.hook("stop_gate.py", {"session_id": "P5", "hook_event_name": "Stop"})
        block = json.loads(r.stdout)
        self.assertEqual(block["decision"], "block")
        self.assertIn("handoff", block["reason"].lower())

        time.sleep(0.02)  # handoff mtime must beat the session marker's
        (self.root / "HANDOFF.md").write_text(
            "# HANDOFF\n\n## Now\nw1 reviewed and complete\n\n## Next\nstart w2\n")
        r = self.hook("stop_gate.py", {"session_id": "P5", "hook_event_name": "Stop"})
        self.assertEqual(r.stdout.strip(), "", "consolidated unit must release stop")

    def test_07_bounded_search_with_shipped_toy_oracle(self):
        # The toy search runs through the search CLI (the engine behind the loop shim). Schema 2
        # has no loop block, so the budget comes from flags, not from adapter.yaml.
        search = self.root / "harness/core/cli/search.py"
        r = subprocess.run([str(self.py), str(search), "run", "--root", str(self.root),
                            "--project", PROJECT, "--oracle", "core.kernel.toy_oracle:ToyOracle",
                            "--max-iterations", "10", "--max-usd", "1.0", "--mock"],
                           cwd=self.root, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("iteration budget 10 reached", r.stdout)

        # registry.jsonl is an append-only EVENT log: each iteration records a `proposal` card
        # and a `verdict` card (kind-tagged lines)
        registry = self.root / f".gategraph/search/{PROJECT}/registry.jsonl"
        events = [json.loads(l) for l in registry.read_text().splitlines() if l.strip()]
        proposals = [e for e in events if e.get("kind") == "proposal"]
        verdicts = [e for e in events if e.get("kind") == "verdict"]
        self.assertEqual(len(proposals), 10, "backstop must bound the run at 10")
        self.assertEqual(len(verdicts), 10, "every iteration must reach a verdict")
        kinds = {v["verdict"] for v in verdicts}
        self.assertEqual(kinds, {"GRADUATE"}, f"toy oracle default is all-pass: {kinds}")
        self.assertTrue((self.root / f".gategraph/search/{PROJECT}/STATUS.md").exists())
        # the search's lock files are released after a clean run: each one is emptied on exit
        for lock in (self.root / ".gategraph/locks").glob("*.lock"):
            self.assertEqual(lock.read_text(), "",
                             f"lock must be released after a clean run: {lock.name}")

        # the loop shim: `validate` is a no-op while the loop is absent (disabled), and `run`
        # refuses, because a schema-2 adapter cannot turn the loop on
        shim = self.root / "harness/core/loop/cli.py"
        r = subprocess.run([sys.executable, str(shim), "validate"], cwd=self.root,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("disabled", r.stdout)
        r = subprocess.run([sys.executable, str(shim), "run", "--mock-workers"], cwd=self.root,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("REFUSING", r.stderr)

    def test_08_zero_hand_edits_to_core_and_check_green(self):
        # THE §17 exit criterion, in checkable form: after the entire lifecycle and the search,
        # the stamped core is bit-identical to the template
        r = subprocess.run([sys.executable, str(TEMPLATE / "gategraph.py"), "diff", str(self.root)],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, f"core drifted during the pilot:\n{r.stdout}{r.stderr}")
        self.assertIn("pristine", r.stdout)

        # the project's own gating self-lint is still green with the search state written
        r = subprocess.run([str(self.py), "-m", "core.kernel.selflint", "--tier", "gating",
                            "--root", "."], cwd=self.root, capture_output=True, text=True,
                           env={**os.environ, "PYTHONPATH": "harness"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class TestPilotSyncStress(unittest.TestCase):
    """§15 Risk 3 on the pilot itself: template release + local core hotfix.
    Runs against a scratch COPY of the template (gategraph upstream writes into the template);
    the real template repo is never touched."""

    HOTFIX_REL = "harness/core/cli/gate.py"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.template = base / "template"
        self.template.mkdir()
        for item in ("gategraph.py", "VERSION", "core", "seed"):
            src = TEMPLATE / item
            if src.is_dir():
                shutil.copytree(src, self.template / item,
                                ignore=shutil.ignore_patterns("__pycache__"))
            else:
                shutil.copy2(src, self.template / item)
        self.project = base / PROJECT
        answers = base / "answers.yaml"
        answers.write_text(yaml.safe_dump(ANSWERS))
        r = self.fh("init", str(self.project), "--answers", str(answers))
        assert r.returncode == 0, r.stdout + r.stderr

    def tearDown(self):
        self._tmp.cleanup()

    def fh(self, *args):
        return subprocess.run(
            [sys.executable, str(self.template / "gategraph.py"), *args],
            capture_output=True, text=True)

    def drift_report(self):
        """`gategraph diff` on the project: its exit code and report (the drift nag)."""
        r = self.fh("diff", str(self.project))
        return r.returncode, r.stdout + r.stderr

    def release_and_hotfix(self):
        """Template ships a release while the project carries a core hotfix."""
        hotfix = self.project / self.HOTFIX_REL
        hotfix.write_text(hotfix.read_text()
                          + "\n# PILOT HOTFIX: battle-tested in consumer-A\n")
        proto = self.template / "core/PROTOCOL.md"
        proto.write_text(proto.read_text() + "\n## Release note\n\nUpstream.\n")
        (self.template / "VERSION").write_text("9.0.0-test\n")
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
        self.assertEqual(manifest["version"], "9.0.0-test")

        # drift nag: `gategraph diff` names exactly the hotfixed file and exits 1
        rc, report = self.drift_report()
        self.assertEqual(rc, 1, "drift present -> diff exits 1")
        self.assertIn("gate.py", report)
        self.assertNotIn("PROTOCOL.md", report,
                         "a pristine file must not be named as drift")

    def test_upstream_promotes_hotfix_and_closes_the_loop(self):
        hotfix = self.release_and_hotfix()
        self.fh("update", str(self.project))

        r = self.fh("upstream", str(self.project), self.HOTFIX_REL)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("PILOT HOTFIX",
                      (self.template / "core/cli/gate.py").read_text(),
                      "hotfix must land in the template")

        # the maintainer resolves the sidecar after promoting
        hotfix.with_name("gate.py.fhnew").unlink()

        rc, report = self.drift_report()
        self.assertEqual(rc, 0, report)
        self.assertIn("pristine and current", report)

        # the promoted template still stamps a live project (self-check green)
        fresh = self.project.parent / "fresh-after-promote"
        answers = self.project.parent / "answers2.yaml"
        answers.write_text(yaml.safe_dump(dict(ANSWERS, project="fresh")))
        r = self.fh("init", str(fresh), "--answers", str(answers))
        self.assertEqual(r.returncode, 0,
                         f"promoted template must still stamp:\n{r.stdout}{r.stderr}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
