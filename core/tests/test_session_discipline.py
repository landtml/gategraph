"""P2 session-discipline tests: stop_gate handoff/escalation conditions and
the session_start context block (router + now + facts + decisions)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import yaml  # noqa: E402
from fixtures import age_file, mkroot, run_cli, run_hook, stop_event  # noqa: E402
from core import handoff, memory, paths  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = mkroot(Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def start(self, slug="u1", sid="S1"):
        r = run_cli("gate.py", "start", "--unit", slug, "--session-id", sid,
                    root=self.root)
        assert r.returncode == 0, r.stderr
        return self.root / "units" / slug

    def complete_unit(self, slug="u1"):
        (self.root / "units" / slug / "note.md").write_text("done\n")
        run_cli("gate.py", "approve", "composed", "--unit", slug, root=self.root)
        run_cli("gate.py", "approve", "checked", "--unit", slug, root=self.root)
        r = run_cli("lint.py", "--unit", slug, "--set-gate", "verified",
                    root=self.root)
        assert r.returncode == 0, r.stdout + r.stderr

    def fresh_handoff(self, sid="S1"):
        """Make HANDOFF.md newer than the session marker."""
        marker = paths.marker_path(self.root, sid)
        if marker.exists():
            age_file(marker, 60)
        (self.root / "HANDOFF.md").write_text(
            "# HANDOFF\n\n## Now\ndone\n\n## Next\nnothing\n")


class TestStopGateHandoff(Base):
    def test_complete_unit_but_stale_handoff_blocks(self):
        self.start(sid="S1")
        self.complete_unit()
        age_file(self.root / "HANDOFF.md", 120)  # written before this session
        r = run_hook("stop_gate.py", stop_event(self.root, "S1"), self.root)
        out = json.loads(r.stdout)
        self.assertEqual(out["decision"], "block")
        self.assertIn("handoff", out["reason"].lower())
        self.assertIn("consolidat", out["reason"].lower())

    def test_complete_unit_with_fresh_handoff_allows(self):
        self.start(sid="S1")
        self.complete_unit()
        self.fresh_handoff("S1")
        r = run_hook("stop_gate.py", stop_event(self.root, "S1"), self.root)
        self.assertEqual(r.stdout.strip(), "", r.stdout + r.stderr)

    def test_mid_workflow_block_also_mentions_stale_handoff(self):
        self.start(sid="S1")
        age_file(self.root / "HANDOFF.md", 120)
        r = run_hook("stop_gate.py", stop_event(self.root, "S1"), self.root)
        out = json.loads(r.stdout)
        self.assertEqual(out["decision"], "block")
        self.assertIn("gate.py approve composed --unit u1", out["reason"])
        self.assertIn("handoff", out["reason"].lower())

    def test_cap_writes_fallback_stub_then_allows(self):
        self.start(sid="S1")
        age_file(self.root / "HANDOFF.md", 120)
        results = [run_hook("stop_gate.py", stop_event(self.root, "S1"), self.root)
                   for _ in range(4)]
        blocks = [r for r in results
                  if r.stdout.strip() and json.loads(r.stdout).get("decision") == "block"]
        self.assertEqual(len(blocks), 3)
        final = results[-1]
        self.assertEqual(final.stdout.strip(), "")  # allowed
        self.assertIn("auto-handoff", final.stderr.lower())
        text = (self.root / "HANDOFF.md").read_text()
        self.assertIn(handoff.AUTO_TAG, text)
        self.assertIn("u1", text)


class TestStopGateEscalation(Base):
    def _set_streak(self, slug: str, n: int):
        (self.root / "harness/state" / f"failstreak-{slug}").write_text(str(n))

    def test_lint_failures_build_the_streak(self):
        self.start(sid="S1")
        (self.root / "units/u1/note.md").write_text("id " + "010190" + "-1234\n")
        for _ in range(2):
            run_cli("lint.py", "--unit", "u1", root=self.root)
        streak = (self.root / "harness/state/failstreak-u1").read_text().strip()
        self.assertEqual(streak, "2")
        # a clean run resets the streak
        (self.root / "units/u1/note.md").write_text("clean now\n")
        run_cli("lint.py", "--unit", "u1", root=self.root)
        streak = (self.root / "harness/state/failstreak-u1").read_text().strip()
        self.assertEqual(streak, "0")

    def test_fail_streak_escalates_instead_of_looping(self):
        self.start(sid="S1")
        self._set_streak("u1", 3)  # == escalation.fail_streak_to_escalate
        r = run_hook("stop_gate.py", stop_event(self.root, "S1"), self.root)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "", "escalation must allow the stop")
        self.assertIn("escalat", r.stderr.lower())
        m = yaml.safe_load((self.root / "units/u1/manifest.yaml").read_text())
        self.assertEqual(m["status"], "escalated")
        self.assertTrue((self.root / "units/u1/escalation.md").is_file())
        self.assertFalse((self.root / "units/.active").exists())

    def test_streak_below_threshold_blocks_normally(self):
        self.start(sid="S1")
        self._set_streak("u1", 2)
        r = run_hook("stop_gate.py", stop_event(self.root, "S1"), self.root)
        out = json.loads(r.stdout)
        self.assertEqual(out["decision"], "block")
        m = yaml.safe_load((self.root / "units/u1/manifest.yaml").read_text())
        self.assertEqual(m["status"], "in_progress")


class TestSessionStartContext(Base):
    def _session_start(self, sid="S1"):
        ev = {"session_id": sid, "cwd": str(self.root),
              "hook_event_name": "SessionStart"}
        return run_hook("session_start.py", ev, self.root)

    def test_prints_router_now_facts_decisions_and_next(self):
        self.start(sid="S1")
        memory.write_now(self.root, "mid-way through sketching the note\n")
        memory.commit_fact(self.root, "notes must cite at least two sources")
        memory.append_decision(self.root, "choice", "went with outline-first")
        r = self._session_start()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("MEMORY.md", r.stdout)                     # router
        self.assertIn("mid-way through sketching", r.stdout)      # working memory
        self.assertIn("cite at least two sources", r.stdout)     # semantic facts
        self.assertIn("went with outline-first", r.stdout)       # episodic tail
        self.assertIn("gate.py approve composed --unit u1", r.stdout)  # next cmd

    def test_prints_state_even_without_active_unit(self):
        memory.commit_fact(self.root, "a durable fact survives sessions")
        r = self._session_start()
        self.assertEqual(r.returncode, 0)
        self.assertIn("durable fact", r.stdout)

    def test_quiet_on_truly_fresh_project(self):
        r = self._session_start()
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
