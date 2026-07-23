"""Unit tests for the memory subsystem: memory.py (4 typed stores),
handoff.py (staleness + consolidation stub), reap.py (state hygiene)."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import yaml  # noqa: E402
from fixtures import age_file, mkroot, run_cli  # noqa: E402
from core import config, handoff, memory, paths  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = mkroot(Path(self._tmp.name))
        self.cfg = config.load(self.root)

    def tearDown(self):
        self._tmp.cleanup()


class TestMemoryStores(Base):
    def test_now_lifecycle(self):
        self.assertEqual(memory.read_now(self.root), "")
        memory.write_now(self.root, "working on n1: sketching the note\n")
        self.assertIn("n1", memory.read_now(self.root))
        memory.clear_now(self.root)
        self.assertEqual(memory.read_now(self.root), "")

    def test_decisions_append_only_ordered(self):
        memory.append_decision(self.root, "choice", "use approach A",
                               rationale="simpler", files=["units/u1/note.md"])
        memory.append_decision(self.root, "fix", "pin the regex")
        decisions = memory.read_decisions(self.root)
        self.assertEqual(len(decisions), 2)
        self.assertEqual(decisions[0]["decision"], "use approach A")
        self.assertEqual(decisions[0]["files"], ["units/u1/note.md"])
        self.assertTrue(all("ts" in d for d in decisions))
        self.assertEqual(len(memory.read_decisions(self.root, last=1)), 1)
        self.assertEqual(memory.read_decisions(self.root, last=1)[0]["decision"],
                         "pin the regex")

    def test_malformed_decision_lines_skipped(self):
        p = self.root / "harness/state/decisions.jsonl"
        p.write_text('{"ts":"t","type":"a","decision":"good"}\nnot json\n')
        decisions = memory.read_decisions(self.root)
        self.assertEqual(len(decisions), 1)

    def test_facts_commit_and_dedupe(self):
        self.assertEqual(memory.read_facts(self.root), [])
        memory.commit_fact(self.root, "the build needs lualatex >= 1.15")
        memory.commit_fact(self.root, "the build needs lualatex >= 1.15")  # dupe
        memory.commit_fact(self.root, "unit slugs are kebab-case")
        facts = memory.read_facts(self.root)
        self.assertEqual(len(facts), 2)
        self.assertTrue(all("at" in f and "text" in f for f in facts))
        # human-editable YAML shape survives a round trip
        raw = yaml.safe_load((self.root / "harness/state/facts.yaml").read_text())
        self.assertIn("facts", raw)

    def test_cli_roundtrip(self):
        r = run_cli("memory.py", "decision", "--type", "choice",
                    "--decision", "went with B", root=self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run_cli("memory.py", "fact", "--text", "B is the default now",
                    root=self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run_cli("memory.py", "now", "--set", "mid-task on u1", root=self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run_cli("memory.py", "show", root=self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        for needle in ("went with B", "B is the default now", "mid-task on u1"):
            self.assertIn(needle, r.stdout)


class TestHandoff(Base):
    def test_fresh_when_session_never_touched(self):
        self.assertFalse(handoff.is_stale(self.root, "S1"))
        self.assertFalse(handoff.is_stale(self.root, None))

    def test_stale_when_touched_after_handoff_write(self):
        paths.touch_marker(self.root, "S1")
        age_file(self.root / "HANDOFF.md", 60)  # handoff written a minute ago
        self.assertTrue(handoff.is_stale(self.root, "S1"))

    def test_fresh_again_after_handoff_updated(self):
        paths.touch_marker(self.root, "S1")
        age_file(paths.marker_path(self.root, "S1"), 60)
        (self.root / "HANDOFF.md").write_text("# HANDOFF\n\n## Now\nupdated\n")
        self.assertFalse(handoff.is_stale(self.root, "S1"))

    def test_missing_handoff_is_stale_for_armed_session(self):
        paths.touch_marker(self.root, "S1")
        (self.root / "HANDOFF.md").unlink()
        self.assertTrue(handoff.is_stale(self.root, "S1"))

    def test_validate_flags_missing_sections(self):
        (self.root / "HANDOFF.md").write_text("just some text, no structure\n")
        problems = handoff.validate(self.root)
        self.assertTrue(any("Now" in p for p in problems))
        self.assertTrue(any("Next" in p for p in problems))

    def test_fallback_stub_appends_never_overwrites(self):
        original = (self.root / "HANDOFF.md").read_text()
        run_cli("gate.py", "start", "--unit", "u1", "--session-id", "S1",
                root=self.root)
        memory.write_now(self.root, "was sketching the intro\n")
        memory.append_decision(self.root, "choice", "kept scope small")
        handoff.write_fallback_stub(self.root, self.cfg)
        text = (self.root / "HANDOFF.md").read_text()
        self.assertTrue(text.startswith(original), "stub must append, not overwrite")
        self.assertIn(handoff.AUTO_TAG, text)
        self.assertIn("u1", text)                       # active unit named
        self.assertIn("composed", text)                  # next gate named
        self.assertIn("was sketching the intro", text)   # now.md compressed in
        self.assertIn("kept scope small", text)         # recent decision
        # a second stub appends again (decay is countable by the lint)
        handoff.write_fallback_stub(self.root, self.cfg)
        self.assertEqual((self.root / "HANDOFF.md").read_text().count(handoff.AUTO_TAG), 2)
        # consolidation is itself an episodic event
        kinds = [d["type"] for d in memory.read_decisions(self.root)]
        self.assertIn("auto-handoff", kinds)


class TestReap(Base):
    def test_reaps_only_stale_pattern_files(self):
        state = self.root / "harness/state"
        old = state / "touched-OLD"
        young = state / "touched-YOUNG"
        keeper = state / "facts.yaml"
        for f in (old, young):
            f.touch()
        keeper.write_text("facts: []\n")
        age_file(old, 8 * 86400)      # past max_age_days: 7
        age_file(keeper, 30 * 86400)  # old but not a reap pattern
        r = run_cli("reap.py", root=self.root)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(old.exists())
        self.assertTrue(young.exists())
        self.assertTrue(keeper.exists())

    def test_dry_run_deletes_nothing(self):
        old = self.root / "harness/state/stopcount-OLD"
        old.touch()
        age_file(old, 8 * 86400)
        r = run_cli("reap.py", "--dry-run", root=self.root)
        self.assertEqual(r.returncode, 0)
        self.assertIn("stopcount-OLD", r.stdout)
        self.assertTrue(old.exists())

    def test_committed_state_is_an_error(self):
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        pid = self.root / "harness/state/touched-COMMITTED"
        pid.touch()
        subprocess.run(["git", "-C", str(self.root), "add", "-f",
                        "harness/state/touched-COMMITTED"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(self.root), "-c", "user.name=t",
                        "-c", "user.email=t@t", "commit", "-qm", "oops"],
                       check=True, capture_output=True)
        r = run_cli("reap.py", root=self.root)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("committed", (r.stdout + r.stderr).lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
